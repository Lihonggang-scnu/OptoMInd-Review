"""Narrow production publication mainline adapter.

This module intentionally owns only the orchestration glue between existing,
already-tested components. It does not reimplement chapter enhancement,
full-manuscript handoff construction, global commander roles, or staged
completion. It converts R4 authoring artifacts into the input-packet contract
consumed by :func:`optomind_research.chapter_asset_enhancer.run_enhancement`,
builds the two manifest shapes expected by the existing handoff and commander
modules, and feeds the established staged-completion runner.

Source boundaries are preserved: R4 evidence packets remain the authoritative
core-evidence input, and explanatory citation work stays in the separate
``background_explanation_only`` ledger produced by the enhancer.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .artifact_store import atomic_write_json


SCHEMA_VERSION = "optomind.publication_mainline_adapter.v1"
ENHANCED_CHAPTER_MD = "ENHANCED_CHAPTER.md"
ENHANCEMENT_REPORT_JSON = "ENHANCEMENT_REPORT.json"
ENHANCEMENT_REUSE_STATE_JSON = "ENHANCEMENT_REUSE_STATE.json"

# Representative-application wiring defaults. They mirror the enhancer's own
# defaults; the adapter still resolves them lazily from the enhancer so a
# single source of truth is preserved. ``application_local_max_results`` is
# additionally clamped to at most six results per local search.
DEFAULT_REPRESENTATIVE_APPLICATIONS_ENABLED = True
DEFAULT_APPLICATION_MAX_TARGETS = 5
DEFAULT_APPLICATION_PER_TARGET_CAP = 6
DEFAULT_APPLICATION_LOCAL_MAX_RESULTS = 6
DEFAULT_APPLICATION_WRITER_TIER = "c2_model"
# Defensive upper bound for representative-application metadata/S2 result settings.
# Values above this are clamped with a warning note.
_APPLICATION_METADATA_UPPER_BOUND = 6
DEFAULT_S2_METADATA_FALLBACK_ENABLED = True
_S2_METADATA_FALLBACK_MAX_RESULTS = _APPLICATION_METADATA_UPPER_BOUND
DEFAULT_ENHANCEMENT_WORKERS = 3

_TRANSIENT_TRANSPORT_ERRORS = frozenset(
    {
        "URLError",
        "TimeoutError",
        "RemoteDisconnected",
        "ConnectionResetError",
        "ConnectionError",
        "ConnectionAbortedError",
        "BrokenPipeError",
    }
)

_COMMON_SCHOLARLY_TOKENS = {
    "model",
    "method",
    "design",
    "algorithm",
    "neural",
    "network",
    "gradient",
    "adaptive",
    "framework",
    "application",
    "approach",
    "technique",
    "system",
    "learning",
    "training",
    "based",
    "using",
    "review",
    "study",
    "paper",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _closed_summary(
    schema_version: str = SCHEMA_VERSION,
    article_title: str = "",
    delivery_gate_path: str = "",
) -> dict[str, Any]:
    """Minimal fail-open summary for early-return paths where no final review exists."""
    return {
        "schema_version": schema_version,
        "delivery_gate": "closed",
        "delivery_gate_path": delivery_gate_path,
        "article_title": article_title,
        "created_at": _now(),
    }


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return default
    return value


def _write_json(path: Path, value: Any) -> None:
    atomic_write_json(path, value)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _sha256_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _build_commander_fail_open_work_order(
    *,
    handoff: Mapping[str, Any],
    output_path: Path,
    error: str,
) -> dict[str, Any]:
    """Persist an honest structural fallback when Commander emits nothing.

    The fallback is deliberately limited to the already-admitted handoff
    order. It gives staged completion a valid, read-only context without
    pretending that global synthesis succeeded or inventing editorial
    decisions.
    """

    raw_order = handoff.get("section_order") or []
    if not isinstance(raw_order, (list, tuple)):
        raw_sections = handoff.get("sections") or {}
        raw_order = list(raw_sections) if isinstance(raw_sections, Mapping) else []
    section_ids = list(
        dict.fromkeys(
            str(value).strip() for value in raw_order if str(value).strip()
        )
    )
    raw_sections = handoff.get("sections") or {}
    section_map = raw_sections if isinstance(raw_sections, Mapping) else {}
    proposed_section_order = [
        {
            "section_id": section_id,
            "position": index,
            "rationale": "preserve the validated handoff order; Commander unavailable",
        }
        for index, section_id in enumerate(section_ids)
    ]
    section_decisions = []
    for section_id in section_ids:
        envelope = section_map.get(section_id) or {}
        title = (
            str(envelope.get("section_title") or "").strip()
            if isinstance(envelope, Mapping)
            else ""
        )
        section_decisions.append(
            {
                "section_id": section_id,
                "decision": "retain",
                "responsibility": title,
                "rationale": "preserve the validated chapter asset without global edits",
            }
        )
    error_text = str(error or "Commander did not produce a work order").strip()
    payload = {
        "schema_version": "optomind.global_manuscript_commander.work_order.v2",
        "status": "failed",
        "mode": "fail_open",
        "model_tier": "",
        "fingerprint": _sha256_text(
            "commander-fail-open|"
            + str(handoff.get("input_fingerprint") or "")
            + "|"
            + ",".join(section_ids)
        ),
        "section_ids": section_ids,
        "manuscript_diagnosis": (
            "Global Commander unavailable; preserve the validated handoff order."
        ),
        "proposed_section_order": proposed_section_order,
        "section_decisions": section_decisions,
        "cross_section_conflicts": [],
        "repeated_paper_role_audit": [],
        "missing_axes": [],
        "structure_gaps": [],
        "structure_candidates": [],
        "selected_story_shape": {},
        "reader_path_findings": [],
        "section_argument_gaps": [],
        "review_structure_gaps": [],
        "gap_value_decisions": [],
        "rejected_gap_candidates": [],
        "coverage_audit_summary": "",
        "visual_work_orders": [],
        "retrieval_gap_proposals": [],
        "proposed_patch_set": [],
        "affected_section_ids": [],
        "next_execution_stages": ["staged_article_completion"],
        "retained_advisory_issues": [error_text],
        "validation_issues": [error_text],
        "fallback_used": True,
        "fallback_reason": "global commander did not persist a work order",
        "read_only_declaration": {
            "chapter_text_changed": False,
            "retrieval_launched": False,
            "note": (
                "Fail-open fallback is limited to the validated handoff order; "
                "no chapter text or evidence binding was changed."
            ),
        },
        "generated_at": _now(),
    }
    _write_json(output_path, payload)
    return payload


def _word_count(value: str) -> int:
    return len(str(value or "").split())


def _sum_usage_tokens(value: Any) -> tuple[int, int]:
    """Recursively sum provider input/output token fields."""

    if not isinstance(value, Mapping):
        return 0, 0
    if "total_input_tokens" in value or "total_output_tokens" in value:
        return (
            int(value.get("total_input_tokens") or 0),
            int(value.get("total_output_tokens") or 0),
        )
    input_tokens = 0
    output_tokens = 0
    for key, nested in value.items():
        if isinstance(nested, Mapping):
            nested_input, nested_output = _sum_usage_tokens(nested)
            input_tokens += nested_input
            output_tokens += nested_output
        elif key == "input_tokens":
            try:
                input_tokens += int(nested or 0)
            except (TypeError, ValueError):
                pass
        elif key == "output_tokens":
            try:
                output_tokens += int(nested or 0)
            except (TypeError, ValueError):
                pass
    return input_tokens, output_tokens


def _sum_usage_cost(value: Any) -> float:
    """Recursively sum provider cost fields, returning 0.0 when unpriced."""

    if not isinstance(value, Mapping):
        return 0.0
    if (
        "estimated_cost_cny" in value
        or "cost_cny" in value
        or "total_estimated_cost_cny" in value
    ):
        try:
            return float(
                value.get("estimated_cost_cny")
                or value.get("cost_cny")
                or value.get("total_estimated_cost_cny")
                or 0.0
            )
        except (TypeError, ValueError):
            return 0.0
    total = 0.0
    for nested in value.values():
        if isinstance(nested, Mapping):
            total += _sum_usage_cost(nested)
    return round(total, 6)


def _resolve_model_name(reference: str) -> str:
    """Resolve a model tier/alias to the concrete pricing-table model name."""

    reference = str(reference or "").strip()
    if not reference:
        return ""
    if "qwen" in reference.casefold():
        return reference
    try:
        from config.qwen_config import get_model_name

        return get_model_name(reference)
    except Exception:
        return reference


_TITLE_QUALITY_BLOCKLIST = re.compile(
    r"\bthe user\b|\buser requires?\b|\buser seeks?\b|\buser asks?\b"
    r"|\bcomprehensive scholarly\b|\bplease (write|generate|produce)\b",
    re.IGNORECASE,
)
_TITLE_MAX_CHARS = 180


def _title_is_acceptable(title: str) -> bool:
    """Return False when a title looks like a verbatim user query.

    Titles longer than _TITLE_MAX_CHARS or containing instruction-style
    language ("The user requires…", "Please generate…") are rejected so the
    helper retries or falls back to the deterministic set instead of writing
    a 600-character user question into the PDF.
    """
    t = str(title or "").strip()
    if not t or len(t) > _TITLE_MAX_CHARS:
        return False
    if _TITLE_QUALITY_BLOCKLIST.search(t):
        return False
    return True


def _generate_title_candidates_via_llm(
    ir: Any,
    model_tier: str,
    max_attempts: int = 3,
) -> list[dict[str, Any]] | None:
    """Call an LLM to generate ranked title candidates for a review paper.

    Returns a list of dicts with at least ``{"title": str, "rank": int}`` keys,
    or ``None`` if all attempts fail.  The caller (plan_review_titles) falls
    back to the deterministic candidate set when this returns None.

    call_qwen_chat contract: first positional arg is agent_name (str), second
    is messages (list), then model_tier as a keyword.  Temperature is a
    keyword; no ``model=`` kwarg exists.
    """

    try:
        from llm.qwen_chat_client import call_qwen_chat
    except Exception:
        return None

    topic = str(getattr(ir, "central_topic", "") or "").strip() or "the field"
    subtype = (
        str(getattr(ir, "review_subtype", "") or "").strip().capitalize()
        or "Review"
    )

    system_msg = (
        "You are an expert academic publication title specialist. "
        "Respond with ONLY valid JSON — no markdown fence, no explanation."
    )
    user_msg = (
        f"Generate exactly 5 candidate titles for a {subtype} paper on: {topic}\n\n"
        "Return a JSON array of 5 objects, each with:\n"
        '  "title": concise academic English title (under 150 characters)\n'
        '  "rank": integer 1-5 (1 = best)\n'
        '  "rationale": one short phrase describing the framing\n\n'
        "Return ONLY the JSON array."
    )

    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            temperature = 0.7 if attempt == 0 else 0.85
            response = call_qwen_chat(
                # call_qwen_chat(agent_name, messages, *, model_tier, temperature, …)
                "publication_title_generator",
                [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                model_tier=model_tier,
                temperature=temperature,
                max_tokens=512,
            )
            raw = (
                (response.get("content") or response.get("text") or "")
                if isinstance(response, dict)
                else str(response or "")
            ).strip()
            # Strip accidental markdown code fence.
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw).strip()
            candidates = json.loads(raw)
            if isinstance(candidates, list):
                good = [
                    c for c in candidates
                    if isinstance(c, dict)
                    and _title_is_acceptable(c.get("title", ""))
                ]
                if good:
                    return good
        except Exception as exc:
            last_exc = exc
    # All attempts failed — caller will use deterministic fallback.
    return None


def _price_tokens(
    model_ref: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Estimate list-price CNY through the shared local cost ledger."""

    from .cost_ledger import estimate_call_cost_cny

    model_name = _resolve_model_name(model_ref)
    if not model_name:
        return 0.0
    return estimate_call_cost_cny(
        model_name,
        max(0, int(input_tokens or 0)),
        max(0, int(output_tokens or 0)),
    )


def _estimate_usage_cost_cny(
    usage: Any,
    *,
    author_tier: str,
    reviewer_tier: str,
) -> tuple[float, bool]:
    """Estimate list-price CNY when the provider recorded no cost.

    Returns ``(estimated_cny, priced)``.  ``priced`` is False only when the
    usage has tokens but no model reference to price them with, so callers can
    keep the transparent ``unaccounted_tokens`` label.
    """

    if not isinstance(usage, Mapping):
        return 0.0, False
    explicit = usage.get("estimated_cost_cny") or usage.get("cost_cny")
    if explicit not in (None, ""):
        try:
            cost = float(explicit)
            if cost > 0:
                return round(cost, 6), True
        except (TypeError, ValueError):
            pass
    total_input, total_output = _sum_usage_tokens(usage)
    if not (total_input or total_output):
        return 0.0, False

    # Price each provider call independently when detailed records survive.
    # Qwen uses request-size brackets, so aggregating several small calls can
    # incorrectly push the total into a more expensive bracket.
    records = usage.get("records") or usage.get("calls")
    if isinstance(records, Mapping):
        record_values = list(records.values())
    elif isinstance(records, list):
        record_values = list(records)
    else:
        record_values = []
    if record_values:
        total = 0.0
        priced_any = False
        for nested in record_values:
            if not isinstance(nested, Mapping):
                continue
            cost, priced = _estimate_usage_cost_cny(
                nested,
                author_tier=author_tier,
                reviewer_tier=reviewer_tier,
            )
            total += cost
            priced_any = priced_any or priced
        if priced_any:
            return round(total, 6), True

    # Editorial author/verifier split preserves the two different rates.
    author = (
        usage.get("author")
        if isinstance(usage.get("author"), Mapping)
        else None
    )
    verifier = (
        usage.get("verifier")
        if isinstance(usage.get("verifier"), Mapping)
        else None
    )
    if author is not None or verifier is not None:
        author_input = int((author or {}).get("input_tokens") or 0)
        author_output = int((author or {}).get("output_tokens") or 0)
        verifier_input = int((verifier or {}).get("input_tokens") or 0)
        verifier_output = int((verifier or {}).get("output_tokens") or 0)
        if (author_input or author_output or verifier_input or verifier_output):
            author_cost = _price_tokens(
                usage.get("model_name")
                or usage.get("model_tier")
                or author_tier,
                author_input,
                author_output,
            )
            verifier_cost = _price_tokens(
                usage.get("verifier_model_tier") or reviewer_tier,
                verifier_input,
                verifier_output,
            )
            return round(author_cost + verifier_cost, 6), True

    reviewers = usage.get("reviewers")
    if isinstance(reviewers, Mapping):
        total = 0.0
        for nested in reviewers.values():
            if not isinstance(nested, Mapping):
                continue
            nested_input, nested_output = _sum_usage_tokens(nested)
            if not (nested_input or nested_output):
                continue
            total += _price_tokens(
                nested.get("model_name")
                or nested.get("model_tier")
                or reviewer_tier,
                nested_input,
                nested_output,
            )
        if total > 0:
            return round(total, 6), True

    return (
        round(
            _price_tokens(
                usage.get("model_name")
                or usage.get("model_tier")
                or author_tier,
                total_input,
                total_output,
            ),
            6,
        ),
        True,
    )


def _usage_cost_accounting(
    usage: Any,
    *,
    author_tier: str,
    reviewer_tier: str,
    reused: bool = False,
) -> tuple[float, str]:
    """Return ``(cost_cny, cost_accounting)`` for one usage record."""

    recorded = _sum_usage_cost(usage)
    if recorded > 0:
        return recorded, "provider_priced"
    total_input, total_output = _sum_usage_tokens(usage)
    if not (total_input or total_output):
        return 0.0, (
            "reused_validated_output" if reused else "no_provider_usage"
        )
    estimated, priced = _estimate_usage_cost_cny(
        usage,
        author_tier=author_tier,
        reviewer_tier=reviewer_tier,
    )
    if priced:
        return estimated, "estimated_from_tokens"
    return 0.0, "unaccounted_tokens"


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return [str(value)] if value not in (None, "") else []
    return [str(item) for item in value if str(item).strip()]


def _normalize_author_names(value: Any) -> list[str]:
    """Normalize mixed string/mapping author entries into display strings."""

    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []

    names: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            name = next(
                (
                    item.get(key)
                    for key in (
                        "name",
                        "display_name",
                        "author",
                        "raw_author_name",
                    )
                    if item.get(key)
                ),
                "",
            )
        elif isinstance(item, str):
            name = item
        else:
            name = ""
        cleaned = str(name or "").strip()
        if cleaned:
            names.append(cleaned)
    return names


def _claim_statement(claim: Mapping[str, Any]) -> str:
    for key in (
        "effective_statement",
        "supported_rewrite",
        "authoring_statement",
        "statement_for_writing",
        "statement",
    ):
        value = str(claim.get(key) or "").strip()
        if value:
            return value
    return ""


def _claim_permission(claim: Mapping[str, Any]) -> str:
    value = str(claim.get("writing_permission") or "").strip()
    if value:
        return value
    support = str(claim.get("support_classification") or "").strip().lower()
    if support in {"hedged", "qualified"}:
        return "hedged_factual_assertion"
    return "factual_assertion"


def _contract_value(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("statement", "title", "description", "purpose"):
            if str(value.get(key) or "").strip():
                return str(value[key]).strip()
    return str(value or "").strip()


def _normalize_claim_record(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    """Normalize either a Phase3/R3 claim or a legacy authoring claim."""

    claim_id = str(raw.get("claim_id") or f"C{index + 1:02d}").strip()
    statement = _claim_statement(raw)
    permission = str(raw.get("writing_permission") or "").strip()
    if not permission:
        permission_status = str(raw.get("permission_status") or "").strip()
        if permission_status in {"qualified_only", "contextual_or_qualified_support"}:
            permission = "hedged_factual_assertion"
        elif permission_status in {"unbound", "discovery_only"}:
            permission = "evidence_gap_only"
        else:
            permission = "factual_assertion"
    return {
        "claim_id": claim_id,
        "statement": statement,
        "statement_for_writing": statement,
        "writing_permission": permission,
        "evidence_binding_status": str(
            raw.get("evidence_binding_status")
            or raw.get("support_status")
            or "direct"
        ),
        "claim_state": str(
            raw.get("claim_state")
            or raw.get("state")
            or "ready_for_write"
        ),
        "permission_status": str(raw.get("permission_status") or ""),
        "supported_components": list(
            raw.get("supported_components")
            or raw.get("claim_components")
            or []
        ),
        "missing_evidence_components": list(
            raw.get("missing_evidence_components") or []
        ),
        "caveats": list(raw.get("caveats") or []),
    }


def _resolve_referenced_path(
    raw: Any,
    *,
    section_work_dir: Path,
    project_root: Path,
) -> Path | None:
    if raw in (None, ""):
        return None
    path = Path(str(raw))
    if path.is_absolute():
        return path
    for base in (section_work_dir, section_work_dir.parent, project_root):
        candidate = base / path
        if candidate.exists():
            return candidate
    return path


def _phase3_authoring_source(
    phase3: Mapping[str, Any],
    *,
    section_work_dir: Path,
    project_root: Path,
) -> tuple[list[dict[str, Any]], Path | None, list[str]]:
    """Read the authoritative Phase3 source ledger and its diagnostics."""

    diagnostics: list[str] = []
    ledger_path = _resolve_referenced_path(
        phase3.get("source_ledger_path"),
        section_work_dir=section_work_dir,
        project_root=project_root,
    )
    if ledger_path is None or not Path(ledger_path).is_file():
        fallback = section_work_dir / "SECTION_SOURCE_LEDGER.json"
        if fallback.is_file():
            ledger_path = fallback
        else:
            diagnostics.append(
                "phase3_source_ledger_missing:"
                f"{phase3.get('source_ledger_path') or ''}"
            )
            return [], None, diagnostics
    ledger = _mapping(_read_json(Path(ledger_path)))
    sources = [
        dict(row)
        for row in ledger.get("sources") or []
        if isinstance(row, Mapping)
    ]
    if not sources:
        diagnostics.append(f"phase3_source_ledger_empty:{ledger_path}")
    return sources, Path(ledger_path), diagnostics


def _resolve_claim_graph(
    phase3: Mapping[str, Any],
    *,
    section_work_dir: Path,
    source_ledger_path: Path | None,
    project_root: Path,
) -> tuple[dict[str, Any] | None, Path | None, list[str]]:
    """Resolve CLAIM_GRAPH.json without topic/BIC-specific hard-coded paths."""

    diagnostics: list[str] = []
    raw_ref = (phase3.get("artifact_refs") or {}).get("CLAIM_GRAPH.json")
    if not isinstance(raw_ref, Mapping):
        raw_ref = {}
    raw_path = str(raw_ref.get("path") or "CLAIM_GRAPH.json")
    path = Path(raw_path)
    candidates: list[Path] = []
    if path.is_absolute():
        try:
            path.resolve().relative_to(project_root.resolve())
            candidates.append(path)
        except ValueError:
            diagnostics.append(
                "claim_graph_absolute_path_outside_project_root:"
                + str(path)
            )
    else:
        bases: list[Path] = []
        project_root_resolved = project_root.resolve()
        for anchor in (
            source_ledger_path,
            _resolve_referenced_path(
                phase3.get("overlay_path"),
                section_work_dir=section_work_dir,
                project_root=project_root,
            ),
            section_work_dir,
        ):
            if anchor is None:
                continue
            current = Path(anchor)
            if current.is_file():
                current = current.parent
            while True:
                try:
                    current.resolve().relative_to(project_root_resolved)
                except ValueError:
                    break
                if current not in bases:
                    bases.append(current)
                if current == project_root_resolved or current.parent == current:
                    break
                current = current.parent
        bases.append(project_root_resolved)
        for base in bases:
            candidate = base / path
            if (
                candidate not in candidates
                and _is_within(candidate, project_root_resolved)
            ):
                candidates.append(candidate)
    claim_graph_path = next(
        (candidate for candidate in candidates if candidate.is_file()),
        None,
    )
    if claim_graph_path is None:
        diagnostics.append(
            "claim_graph_missing:"
            + raw_path
            + ":candidate_bases="
            + str(len(candidates))
        )
        return None, None, diagnostics
    try:
        value = _read_json(claim_graph_path)
    except Exception as exc:
        diagnostics.append(
            f"claim_graph_unreadable:{type(exc).__name__}:{claim_graph_path}"
        )
        return None, claim_graph_path, diagnostics
    if not isinstance(value, Mapping):
        diagnostics.append("claim_graph_root_not_object")
        return None, claim_graph_path, diagnostics
    expected_sha = str(raw_ref.get("sha256") or "")
    if expected_sha:
        actual_sha = _sha256_file(claim_graph_path)
        if actual_sha != expected_sha:
            diagnostics.append(
                "claim_graph_sha256_mismatch_rejected:"
                f"{actual_sha}:{expected_sha}"
            )
            return None, claim_graph_path, diagnostics
    return value, claim_graph_path, diagnostics


def _merge_claim_graph_into_claims(
    claims: Sequence[Mapping[str, Any]],
    claim_graph: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    nodes = claim_graph.get("nodes")
    if not isinstance(nodes, list):
        nodes = claim_graph.get("claims")
    if not isinstance(nodes, list):
        return [dict(claim) for claim in claims], [
            "claim_graph_nodes_not_list"
        ]
    by_id: dict[str, dict[str, Any]] = {}
    for raw in nodes:
        if not isinstance(raw, Mapping):
            continue
        claim_id = str(raw.get("claim_id") or "").strip()
        if claim_id:
            by_id[claim_id] = dict(raw)
    merged: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    for raw in claims:
        claim = dict(raw)
        claim_id = str(claim.get("claim_id") or "").strip()
        node = by_id.get(claim_id)
        if node is None:
            diagnostics.append(f"claim_graph_missing_claim:{claim_id}")
            merged.append(claim)
            continue
        for key in (
            "evidence_spans",
            "evidence_component_map",
            "verified_quotes",
        ):
            if node.get(key):
                claim[key] = node[key]
        merged.append(claim)
    return merged, diagnostics


def _load_exact_chunk_text(
    *,
    local_kb_path: Path | None,
    chunk_id: str,
    paper_id: str,
) -> dict[str, Any] | None:
    if not local_kb_path or not Path(local_kb_path).is_file():
        return None
    conn = sqlite3.connect(str(local_kb_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT text, evidence_level, source_kind, provenance_json,
                   content_depth, use_permission, context_complete, scope_fit
            FROM text_chunks
            WHERE chunk_id=? AND paper_id=?
            LIMIT 1
            """,
            (str(chunk_id), str(paper_id)),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "text": str(row["text"] or ""),
        "evidence_level": str(row["evidence_level"] or ""),
        "source_kind": str(row["source_kind"] or ""),
        "provenance_json": str(row["provenance_json"] or ""),
        "content_depth": str(row["content_depth"] or ""),
        "use_permission": str(row["use_permission"] or ""),
        "context_complete": bool(row["context_complete"]),
        "scope_fit": str(row["scope_fit"] or ""),
    }


def _phase3_claim_evidence_rows(
    claims: Sequence[Mapping[str, Any]],
    source_rows: Sequence[Mapping[str, Any]],
    *,
    local_kb_path: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build core evidence rows from real claim-to-chunk/source bindings."""

    by_chunk: dict[str, list[dict[str, Any]]] = {}
    for source in source_rows:
        for chunk_id in source.get("canonical_chunk_ids") or []:
            chunk_id = str(chunk_id or "").strip()
            if chunk_id:
                by_chunk.setdefault(chunk_id, []).append(source)

    rows: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for claim in claims:
        claim_id = str(claim.get("claim_id") or "").strip()
        if not claim_id:
            continue
        preferred_paper_ids = {
            str(paper_id)
            for paper_id in (
                claim.get("core_paper_ids")
                or claim.get("supporting_paper_ids")
                or []
            )
            if str(paper_id).strip()
        }
        chunk_ids: list[str] = []
        for key in (
            "core_chunk_ids",
            "factual_support_chunk_ids",
            "supporting_text_chunk_ids",
            "contextual_support_chunk_ids",
        ):
            for chunk_id in claim.get(key) or []:
                chunk_id = str(chunk_id or "").strip()
                if chunk_id and chunk_id not in chunk_ids:
                    chunk_ids.append(chunk_id)
        evidence_spans = claim.get("evidence_spans") or []
        spans_by_chunk: dict[str, dict[str, Any]] = {}
        for span in evidence_spans:
            if not isinstance(span, Mapping):
                continue
            chunk_id = str(span.get("chunk_id") or "").strip()
            if chunk_id:
                spans_by_chunk[chunk_id] = dict(span)
        unresolved_chunk_ids: list[str] = []
        for chunk_id in chunk_ids:
            matches = by_chunk.get(chunk_id) or []
            if not matches:
                unresolved_chunk_ids.append(chunk_id)
                continue
            source = next(
                (
                    candidate
                    for candidate in matches
                    if str(candidate.get("paper_id") or "") in preferred_paper_ids
                ),
                matches[0],
            )
            paper_id = str(source.get("paper_id") or "").strip()
            key = (claim_id, paper_id, chunk_id)
            if key in seen:
                continue
            span = spans_by_chunk.get(chunk_id)
            verified_quote = ""
            span_source = ""
            if span is not None and str(span.get("quote_verified") or "").lower() in {
                "true",
                "1",
                "yes",
            }:
                verified_quote = str(span.get("quote") or "").strip()
                span_source = "verified_quote"
            fallback_chunk = None
            if not verified_quote:
                fallback_chunk = _load_exact_chunk_text(
                    local_kb_path=local_kb_path,
                    chunk_id=chunk_id,
                    paper_id=paper_id,
                )
                if fallback_chunk and str(fallback_chunk.get("text") or "").strip():
                    span_source = "full_bound_chunk_fallback"
            if not verified_quote and not (
                fallback_chunk and str(fallback_chunk.get("text") or "").strip()
            ):
                unresolved_chunk_ids.append(chunk_id)
                continue
            seen.add(key)
            exact_spans = (
                [verified_quote]
                if verified_quote
                else [str(fallback_chunk["text"]).strip()]
            )
            content_depth = str(source.get("content_depth") or "fulltext").strip()
            materialization_route = str(
                source.get("materialization_route") or "not_materialized"
            ).strip()
            rows.append(
                {
                    "claim_id": claim_id,
                    "paper_id": paper_id,
                    "chunk_id": chunk_id,
                    "exact_spans": exact_spans,
                    "visual_refs": [],
                    "support_relation": "component_support",
                    "limitations": list(source.get("not_usable_for") or []),
                    "evidence_level": (
                        fallback_chunk.get("evidence_level")
                        if fallback_chunk
                        else content_depth
                    )
                    or content_depth,
                    "source_kind": (
                        materialization_route
                        or content_depth
                        or "structured_snippet"
                    ),
                    "scope_fit": str(source.get("scope_fit") or "direct"),
                    "retrieval_role": "evidence_candidate",
                    "source_title": str(source.get("title") or ""),
                    "span_source": span_source,
                    "span_fallback": bool(fallback_chunk),
                }
            )
            provenance.append(
                {
                    "claim_id": claim_id,
                    "paper_id": paper_id,
                    "chunk_id": chunk_id,
                    "span_source": span_source,
                    "quote_verified": bool(verified_quote),
                    "source_locator": (
                        span.get("source_locator")
                        if span is not None
                        else None
                    ),
                    "quote_match_mode": (
                        span.get("quote_match_mode")
                        if span is not None
                        else ""
                    ),
                    "fallback_chunk_provenance": (
                        fallback_chunk.get("provenance_json")
                        if fallback_chunk
                        else ""
                    ),
                }
            )
        if unresolved_chunk_ids:
            unresolved.append(
                {
                    "claim_id": claim_id,
                    "reason": (
                        "core_chunk_not_resolved_or_no_verified_span"
                    ),
                    "chunk_ids": unresolved_chunk_ids,
                }
            )
    return rows, unresolved, provenance


def _expand_local_query_terms(query: str) -> list[str]:
    """Copy the bounded local query expansion used by the standalone CLI."""

    phrase = " ".join(str(query or "").split())
    tokens = [
        token
        for token in re.findall(r"[A-Za-z0-9]+", phrase)
        if len(token) >= 3
    ]
    distinctive = [
        token
        for token in tokens
        if token.lower() not in _COMMON_SCHOLARLY_TOKENS
        and (
            token.isupper()
            or any(char.isdigit() for char in token)
            or len(token) >= 8
        )
    ]
    if not distinctive:
        distinctive = [
            token
            for token in tokens
            if token.lower() not in _COMMON_SCHOLARLY_TOKENS
        ][:3]
    phrases: list[str] = []
    if len(tokens) <= 5:
        phrases.append(phrase)
    for n in (2, 3):
        for index in range(max(0, len(tokens) - n + 1)):
            phrases.append(" ".join(tokens[index : index + n]))
    terms = list(dict.fromkeys([*distinctive[:3], *phrases[:3]]))
    return [term for term in terms if term][:7]


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    resolved = Path(db_path).resolve().as_posix()
    return sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)


def _sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _json_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _search_abstract_papers(
    conn: sqlite3.Connection,
    terms: list[str],
    max_results: int,
) -> list[dict[str, Any]]:
    rows_by_id: dict[str, dict[str, Any]] = {}
    for term in terms[:12]:
        like = f"%{term}%"
        rows = conn.execute(
            """
            SELECT paper_id, doi, semantic_scholar_id, openalex_id, title,
                   authors_json, year, venue, abstract, citation_count,
                   open_access, pdf_url, landing_page_url, source_apis_json,
                   query_used_json, matched_keywords_json, topic_tags_json,
                   embedding_id, raw_json
            FROM abstract_papers
            WHERE title LIKE ? OR abstract LIKE ? OR venue LIKE ?
            ORDER BY year DESC
            LIMIT ?
            """,
            (like, like, like, max(10, int(max_results))),
        ).fetchall()
        for row in rows:
            raw = _mapping(
                json.loads(row["raw_json"] or "{}")
                if row["raw_json"]
                else {}
            )
            rows_by_id[row["paper_id"]] = {
                "paper_id": row["paper_id"],
                "doi": row["doi"] or "",
                "semantic_scholar_id": row["semantic_scholar_id"] or "",
                "openalex_id": row["openalex_id"] or "",
                "title": row["title"] or "",
                "authors": _normalize_author_names(
                    _json_list(row["authors_json"])
                ),
                "year": row["year"],
                "venue": row["venue"] or "",
                "abstract": row["abstract"] or "",
                "citation_count": row["citation_count"],
                "open_access": (
                    None
                    if row["open_access"] is None
                    else bool(row["open_access"])
                ),
                "pdf_url": row["pdf_url"] or "",
                "landing_page_url": row["landing_page_url"] or "",
                "source_apis": _json_list(row["source_apis_json"]),
                "query_used": _json_list(row["query_used_json"]),
                "matched_keywords": _json_list(
                    row["matched_keywords_json"]
                ),
                "topic_tags": _json_list(row["topic_tags_json"]),
                "embedding_id": row["embedding_id"] or "",
                "raw": raw,
                "source_audit": raw.get("source_audit") or {},
            }
    return list(rows_by_id.values())[: int(max_results)]


def _search_papers(
    conn: sqlite3.Connection,
    terms: list[str],
    max_results: int,
) -> list[dict[str, Any]]:
    rows_by_id: dict[str, dict[str, Any]] = {}
    for term in terms[:12]:
        like = f"%{term}%"
        rows = conn.execute(
            """
            SELECT paper_id, doi, title, year, venue, search_text, raw_json
            FROM papers
            WHERE title LIKE ? OR search_text LIKE ? OR raw_json LIKE ?
            ORDER BY year DESC
            LIMIT ?
            """,
            (like, like, like, max(10, int(max_results))),
        ).fetchall()
        for row in rows:
            raw = _mapping(
                json.loads(row["raw_json"] or "{}")
                if row["raw_json"]
                else {}
            )
            rows_by_id[row["paper_id"]] = {
                "paper_id": row["paper_id"],
                "doi": row["doi"] or "",
                "title": row["title"] or "",
                "year": row["year"],
                "venue": row["venue"] or "",
                "authors": _normalize_author_names(
                    raw.get("authors") or []
                ),
                "abstract": (
                    raw.get("abstract")
                    or row["search_text"]
                    or ""
                ),
            }
    return list(rows_by_id.values())[: int(max_results)]


class _LocalMetadataCallback:
    """Read-only long-term local metadata callback."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.is_file():
            raise ValueError(
                "Local metadata store does not exist; "
                f"publication mainline will not create one: {self.db_path}"
            )

    def __call__(self, query: str, max_results: int) -> list[dict[str, Any]]:
        terms = _expand_local_query_terms(query)
        conn = _connect_read_only(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            if _sqlite_table_exists(conn, "abstract_papers"):
                try:
                    abstract_rows = _search_abstract_papers(
                        conn, terms, max_results
                    )
                except sqlite3.OperationalError:
                    abstract_rows = []
                if abstract_rows:
                    return abstract_rows
            if _sqlite_table_exists(conn, "papers"):
                return _search_papers(conn, terms, max_results)
            return []
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def close(self) -> None:
        return None


def build_enhancer_input_packet(
    *,
    section_work_dir: Path,
    blueprint_section: Mapping[str, Any],
    blueprint: Mapping[str, Any],
    output_path: Path,
    project_root: Path | None = None,
    local_kb_path: Path | None = None,
) -> Path:
    """Convert durable R4 authoring artifacts into an enhancer input packet.

    The packet is written as ``input_packet.json`` in the section's enhancer
    output directory. It uses only R4-authored evidence as core evidence.
    Explanatory citations are not mixed into this packet; the enhancer builds
    them separately from its own metadata/search callbacks.
    """

    section_work_dir = Path(section_work_dir)
    if not section_work_dir.is_dir():
        raise ValueError(f"section work dir missing: {section_work_dir}")
    section_id = str(
        blueprint_section.get("section_id")
        or section_work_dir.name
    ).strip()
    if not section_id:
        raise ValueError("blueprint_section.section_id must be non-empty")

    context = _mapping(
        _read_json(section_work_dir / "SECTION_AUTHORING_CONTEXT.json")
    )
    argument_plan = _mapping(
        _read_json(section_work_dir / "SECTION_ARGUMENT_PLAN.json")
    )
    evidence_packet = _mapping(
        _read_json(section_work_dir / "SECTION_EVIDENCE_PACKET.json")
    )
    source_ledger = _mapping(
        _read_json(section_work_dir / "SECTION_SOURCE_LEDGER.json")
    )
    phase3 = _mapping(
        _read_json(section_work_dir / "PHASE3_AUTHORING_CONTEXT.json")
    )

    title = str(
        context.get("section_title")
        or blueprint_section.get("title")
        or blueprint_section.get("section_title")
        or section_id
    ).strip()

    section_contract = dict(context.get("section_contract") or {})
    section_contract.setdefault("section_id", section_id)
    section_contract.setdefault("title", title)
    section_contract.setdefault(
        "central_thesis",
        _contract_value(
            context.get("chapter_argument")
            or blueprint_section.get("chapter_argument")
            or blueprint_section.get("argument_role")
        ),
    )
    section_contract.setdefault(
        "section_purpose",
        _contract_value(
            context.get("section_role")
            or blueprint_section.get("section_purpose")
            or blueprint_section.get("synthesis_task")
        ),
    )
    section_contract.setdefault(
        "argument_role",
        _contract_value(
            context.get("section_role")
            or blueprint_section.get("argument_role")
        ),
    )
    section_contract.setdefault(
        "key_questions",
        list(blueprint_section.get("key_questions") or []),
    )
    section_contract.setdefault(
        "scope_guardrails",
        list(context.get("scope_guardrails") or []),
    )
    section_contract.setdefault(
        "forbidden_overclaims",
        list(blueprint_section.get("forbidden_overclaims") or []),
    )

    phase3_claims = phase3.get("claims")
    phase3_active = bool(
        isinstance(phase3_claims, list)
        and phase3_claims
        and phase3.get("section_id") == section_id
    )
    unresolved_bindings: list[dict[str, Any]] = []
    phase3_diagnostics: list[str] = []
    phase3_ledger_path: Path | None = None

    if phase3_active:
        authorable_claim_ids = {
            str(claim_id)
            for claim_id in phase3.get("authorable_claim_ids") or []
            if str(claim_id).strip()
        }
        if authorable_claim_ids:
            phase3_claims = [
                claim
                for claim in phase3_claims
                if isinstance(claim, Mapping)
                and str(claim.get("claim_id") or "") in authorable_claim_ids
            ]
        claims_raw = [
            claim
            for claim in phase3_claims
            if isinstance(claim, Mapping)
        ]
        (
            literature_sources,
            phase3_ledger_path,
            source_diagnostics,
        ) = _phase3_authoring_source(
            phase3,
            section_work_dir=section_work_dir,
            project_root=Path(project_root or section_work_dir),
        )
        phase3_diagnostics.extend(source_diagnostics)
        claim_graph, claim_graph_path, claim_graph_diagnostics = (
            _resolve_claim_graph(
                phase3,
                section_work_dir=section_work_dir,
                source_ledger_path=phase3_ledger_path,
                project_root=Path(project_root or section_work_dir),
            )
        )
        phase3_diagnostics.extend(claim_graph_diagnostics)
        if claim_graph is not None:
            claims_raw, merge_diagnostics = _merge_claim_graph_into_claims(
                claims_raw,
                claim_graph,
            )
            phase3_diagnostics.extend(merge_diagnostics)
        claims = [
            _normalize_claim_record(raw, index)
            for index, raw in enumerate(claims_raw)
        ]
        (
            evidence_rows,
            unresolved_bindings,
            phase3_span_provenance,
        ) = _phase3_claim_evidence_rows(
            claims_raw,
            literature_sources,
            local_kb_path=local_kb_path,
        )
    else:
        phase3_span_provenance = []
    if not phase3_active:
        claims_raw = context.get("claims")
        if not isinstance(claims_raw, list) or not claims_raw:
            claims_raw = blueprint_section.get("claims") or []
        claims = [
            _normalize_claim_record(raw, index)
            for index, raw in enumerate(claims_raw)
            if isinstance(raw, Mapping)
        ]
        evidence_rows = []
        for item in evidence_packet.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            paper_id = str(item.get("paper_id") or "").strip()
            chunk_id = str(item.get("chunk_id") or "").strip()
            if not paper_id or not chunk_id:
                continue
            claim_ids = _as_string_list(
                item.get("claim_ids") or item.get("claim_id")
            )
            if not claim_ids:
                claim_ids = [""]
            for claim_id in claim_ids:
                evidence_rows.append(
                    {
                        "claim_id": claim_id,
                        "paper_id": paper_id,
                        "chunk_id": chunk_id,
                        "exact_spans": list(item.get("exact_spans") or []),
                        "visual_refs": list(item.get("visual_refs") or []),
                        "support_relation": "component_support",
                        "limitations": list(
                            item.get("not_usable_for") or []
                        ),
                        "evidence_level": str(
                            item.get("evidence_level") or "fulltext"
                        ),
                        "source_kind": "canonical_section_evidence",
                        "scope_fit": str(
                            item.get("scope_fit") or "in_domain"
                        ),
                        "retrieval_role": "evidence_candidate",
                        "source_title": str(
                            item.get("paper_title") or ""
                        ),
                    }
                )
        source_rows = source_ledger.get("sources") or []
        literature_sources = [
            dict(row)
            for row in source_rows
            if isinstance(row, Mapping)
        ]
    excluded_phase3_claim_ids = [
        str(claim_id)
        for claim_id in phase3.get("excluded_claim_ids") or []
        if str(claim_id).strip()
    ] if phase3_active else []
    unresolved_claim_ids = [
        str(row.get("claim_id") or "")
        for row in unresolved_bindings
        if str(row.get("claim_id") or "").strip()
    ]
    unready_claim_ids = list(
        dict.fromkeys(excluded_phase3_claim_ids + unresolved_claim_ids)
    )

    manuscript_context = {
        "source_section_title": title,
        "research_context": {
            "user_question": str(
                blueprint.get("input_context", {}).get("user_question")
                or blueprint.get("user_question")
                or ""
            ),
            "scope_definition": str(
                context.get("scope_guardrails")
                or section_contract.get("scope_guardrails")
                or ""
            ),
            "global_review_thesis": str(
                context.get("full_review_argument")
                or blueprint.get("full_review_argument")
                or blueprint.get("review_thesis")
                or ""
            ),
            "global_narrative_strategy": str(
                context.get("section_role") or ""
            ),
        },
        "current_section_boundary_contract": {
            "section_id": section_id,
            "title": title,
            "handoff_from_previous": str(
                (context.get("transition_contract") or {}).get(
                    "transition_from_previous"
                )
                or blueprint_section.get("transition_from_previous")
                or ""
            ),
            "handoff_to_next": str(
                (context.get("transition_contract") or {}).get(
                    "transition_to_next"
                )
                or blueprint_section.get("transition_to_next")
                or ""
            ),
        },
        "full_section_workplan": list(argument_plan.get("paragraphs") or []),
        "sibling_section_responsibilities": [
            {
                "section_id": str(section.get("section_id") or ""),
                "title": str(section.get("title") or ""),
                "summary": _contract_value(section.get("argument_role")),
            }
            for section in blueprint.get("sections") or []
            if isinstance(section, Mapping)
            and str(section.get("section_id") or "") != section_id
        ],
        "reviewer_comments_retained": [],
        "excluded_unready_claim_ids": list(
            unready_claim_ids
            if phase3_active
            else evidence_packet.get("uncovered_claim_ids") or []
        ),
        "write_gate": {"allowed_to_write": True},
        "evidence_provenance": {
            "authoritative_input_packet": (
                "phase3_claim_bindings"
                if phase3_active
                else "section_evidence_packet"
            ),
            "explanatory_trust_boundary": "background_explanation_only",
        },
    }

    packet = {
        "schema_version": "publication_mainline.enhancer_input.v1",
        "section_id": section_id,
        "section_contract": section_contract,
        "claims": claims,
        "evidence_packets": evidence_rows,
        "contradictions": list(context.get("contradictions") or []),
        "open_questions": list(
            argument_plan.get("open_questions")
            or blueprint_section.get("open_questions")
            or []
        ),
        "transition_contract": dict(context.get("transition_contract") or {}),
        "uncited_load_bearing_claim_ids": list(
            unready_claim_ids
            if phase3_active
            else evidence_packet.get("uncovered_claim_ids") or []
        ),
        "unresolved_bindings": unresolved_bindings,
        "phase3_span_provenance": phase3_span_provenance,
        "phase3_diagnostics": phase3_diagnostics,
        "source_ledger_path": (
            str(phase3_ledger_path) if phase3_ledger_path else ""
        ),
        "visual_evidence": list(
            phase3.get("visual_chunk_ids")
            or context.get("visual_chunk_ids")
            or []
        ),
        "visual_gap_plan": list(blueprint_section.get("visual_gap_plan") or []),
        "manuscript_context": manuscript_context,
        "literature_coverage": {
            "sources": literature_sources,
            "paper_ids": sorted(
                {
                    str(row.get("paper_id") or "").strip()
                    for row in literature_sources
                    if str(row.get("paper_id") or "").strip()
                }
            ),
            "evidence_chunk_ids": sorted(
                {
                    str(item.get("chunk_id") or "").strip()
                    for item in evidence_rows
                    if str(item.get("chunk_id") or "").strip()
                }
            ),
        },
    }
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    _write_json(Path(output_path), packet)
    return Path(output_path)


def _default_enhancement_runner(**kwargs: Any) -> dict[str, Any]:
    from optomind_research.chapter_asset_enhancer import run_enhancement

    return run_enhancement(**kwargs)


def _call_enhancement_runner(
    runner: Callable[..., dict[str, Any]],
    **kwargs: Any,
) -> dict[str, Any]:
    """Call a real or injected enhancement runner without forcing test seams."""

    try:
        parameters = inspect.signature(runner).parameters
    except (TypeError, ValueError):
        return runner(**kwargs)
    accepted = {
        key: value
        for key, value in kwargs.items()
        if key in parameters or any(
            param.kind == inspect.Parameter.VAR_KEYWORD
            for param in parameters.values()
        )
    }
    return runner(**accepted)


def _build_commander_manifest(
    *,
    project_root: Path,
    handoff: Mapping[str, Any],
    output_path: Path,
) -> Path:
    rows: list[dict[str, Any]] = []
    sections = handoff.get("sections") or {}
    for section_id in handoff.get("section_order") or []:
        envelope = sections.get(section_id)
        if not isinstance(envelope, Mapping):
            continue
        content_status = str(envelope.get("content_status") or "enhanced")
        enhanced = envelope.get("enhanced_chapter") or {}
        packet = envelope.get("authoritative_input_packet") or {}
        ledger = envelope.get("explanatory_citation_ledger") or {}

        def resolve(record: Mapping[str, Any]) -> Path:
            raw = str(record.get("path") or "")
            path = Path(raw)
            if not path.is_absolute():
                path = project_root / path
            return path

        enhanced_path = resolve(enhanced)
        packet_path = resolve(packet)
        ledger_path = resolve(ledger) if ledger.get("path") else None
        row: dict[str, Any] = {
            "section_id": str(section_id),
            "content_status": content_status,
            "english_draft_path": str(enhanced_path),
            "input_packet_path": str(packet_path),
        }
        if ledger_path is not None:
            row["explanatory_citation_ledger_path"] = str(ledger_path)
        rows.append(row)
    manifest = {
        "schema_version": "optomind.global_manuscript_commander.manifest.v2",
        "sections": rows,
    }
    _write_json(output_path, manifest)
    return output_path


def _commander_usage(commander_dir: Path) -> tuple[float, int, int, dict[str, Any]]:
    reviews = _mapping(
        _read_json(commander_dir / "role_reviews.json")
    )
    roles = reviews.get("roles") or {}
    cost_cny = 0.0
    input_tokens = 0
    output_tokens = 0
    role_usage: dict[str, Any] = {}
    for role, record in roles.items():
        if not isinstance(record, Mapping):
            continue
        usage = record.get("usage") or {}
        role_input, role_output = _sum_usage_tokens(usage)
        role_cost = _sum_usage_cost(usage)
        cost_cny += role_cost
        input_tokens += role_input
        output_tokens += role_output
        role_usage[str(role)] = {
            "cost_cny": round(role_cost, 6),
            "input_tokens": role_input,
            "output_tokens": role_output,
        }
    return round(cost_cny, 6), input_tokens, output_tokens, role_usage


def _staged_usage(
    state: Any,
    *,
    author_tier: str,
    reviewer_tier: str,
) -> tuple[int, int, float, dict[str, Any]]:
    input_tokens = 0
    output_tokens = 0
    cost_cny = 0.0
    by_stage: dict[str, Any] = {}
    for stage, record in (state.stages or {}).items():
        usage = getattr(record, "usage", {}) or {}
        stage_input, stage_output = _sum_usage_tokens(usage)
        stage_cost, stage_accounting = _usage_cost_accounting(
            usage,
            author_tier=author_tier,
            reviewer_tier=reviewer_tier,
        )
        input_tokens += stage_input
        output_tokens += stage_output
        cost_cny += stage_cost
        by_stage[str(stage)] = {
            "input_tokens": stage_input,
            "output_tokens": stage_output,
            "cost_cny": round(stage_cost, 6),
            "cost_accounting": stage_accounting,
        }
    return input_tokens, output_tokens, round(cost_cny, 6), by_stage


def _build_downstream_section_dir(
    *,
    staged_work_dir: Path,
    staged_state: Any,
    output_dir: Path,
) -> Path:
    """Materialize per-section staged final drafts for visual downstream use."""

    output_dir.mkdir(parents=True, exist_ok=True)
    editorial = staged_state.stages.get("editorial_revision")
    manuscript = {}
    if editorial is not None:
        manuscript = _mapping(_mapping(editorial.payload).get("manuscript"))
    by_section: dict[str, str] = {}
    for entry in manuscript.get("assembled") or []:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("kind") or "") != "section":
            continue
        section_id = str(entry.get("target_id") or "")
        if not section_id:
            continue
        parts: list[str] = []
        if entry.get("blocks") is not None:
            for block in entry.get("blocks") or []:
                if isinstance(block, Mapping) and str(block.get("text") or "").strip():
                    parts.append(str(block["text"]))
        elif str(entry.get("text") or "").strip():
            parts.append(str(entry["text"]))
        by_section[section_id] = "\n\n".join(parts)

    for section_id, text in by_section.items():
        section_dir = output_dir / str(section_id)
        section_dir.mkdir(parents=True, exist_ok=True)
        (section_dir / "SECTION_DRAFT_EN.md").write_text(
            text,
            encoding="utf-8",
        )
    return output_dir


def _editorial_accounting(staged_state: Any) -> dict[str, Any]:
    editorial = staged_state.stages.get("editorial_revision")
    if editorial is None:
        return {
            "work_item_count": 0,
            "accepted_applied_count": 0,
            "no_change_count": 0,
            "rejected_unsafe_count": 0,
            "failed_empty_count": 0,
            "blocking_unresolved": [],
            "closure_completed": False,
        }
    payload = _mapping(editorial.payload)
    audit = _mapping(payload.get("audit"))
    records = audit.get("records") or []
    blocking = audit.get("blocking_unresolved") or []
    accepted_applied = 0
    no_change = 0
    rejected_unsafe = 0
    failed_empty = 0
    applied_ids = {
        str(item)
        for item in (audit.get("applied_revision_ids") or [])
        if str(item).strip()
    }
    unapplied_ids = {
        str(item)
        for item in (audit.get("unapplied_accepted_revision_ids") or [])
        if str(item).strip()
    }
    for raw in records:
        if not isinstance(raw, Mapping):
            continue
        status = str(raw.get("status") or "")
        work_item_id = str(raw.get("work_item_id") or "")
        original = str(raw.get("original_text") or "")
        revised = str(raw.get("revised_text") or "")
        word_delta = _word_count(revised) - _word_count(original)
        changed_hash = bool(original != revised)
        application_status = str(raw.get("application_status") or "")
        if status == "accepted":
            # New runs record the actual assembly result.  Do not infer
            # application from word-count changes: a valid rewrite can keep
            # the same number of words.
            if (
                application_status == "applied"
                or work_item_id in applied_ids
            ):
                accepted_applied += 1
            elif (
                application_status == "unapplied"
                or work_item_id in unapplied_ids
            ):
                rejected_unsafe += 1
            # Compatibility with pre-materialization audit records.
            elif not application_status and not applied_ids and not unapplied_ids:
                if changed_hash and word_delta != 0:
                    accepted_applied += 1
                else:
                    no_change += 1
            else:
                failed_empty += 1
        elif status == "rejected" and str(raw.get("reason") or "").startswith(
            "author_returned_no_change"
        ):
            no_change += 1
        elif status == "rejected" and str(raw.get("reason") or "").startswith(
            ("verifier_rejected:", "ref_markers_changed")
        ):
            rejected_unsafe += 1
        elif status in {"failed", ""} or not revised:
            failed_empty += 1
        else:
            rejected_unsafe += 1
    work_item_count = int(audit.get("work_item_count") or len(records))
    accepted_count = sum(
        1
        for raw in records
        if isinstance(raw, Mapping)
        and str(raw.get("status") or "") == "accepted"
    )
    terminal_count = sum(
        1
        for raw in records
        if isinstance(raw, Mapping)
        and str(raw.get("status") or "") in {"accepted", "rejected"}
    )
    closure_completed = (
        not blocking
        and len(records) == work_item_count
        and terminal_count == work_item_count
        and accepted_applied == accepted_count
        and not unapplied_ids
    )
    return {
        "work_item_count": work_item_count,
        "accepted_applied_count": accepted_applied,
        "accepted_count": accepted_count,
        "unapplied_accepted_count": len(unapplied_ids),
        "no_change_count": no_change,
        "rejected_unsafe_count": rejected_unsafe,
        "failed_empty_count": failed_empty,
        "blocking_unresolved": blocking,
        "closure_completed": closure_completed,
    }


@dataclass
class PublicationMainlineResult:
    status: str
    completed_stage: str
    enhanced_sections: list[str] = field(default_factory=list)
    failed_sections: list[dict[str, Any]] = field(default_factory=list)
    manifest_path: Path | None = None
    handoff_path: Path | None = None
    commander_manifest_path: Path | None = None
    commander_work_order_path: Path | None = None
    commander_summary: dict[str, Any] = field(default_factory=dict)
    staged_context_dir: Path | None = None
    staged_state_path: Path | None = None
    staged_state: Any = None
    final_review_path: Path | None = None
    downstream_review_work_dir: Path | None = None
    editorial_closure_completed: bool = False
    fail_open_issues: list[str] = field(default_factory=list)
    accounting: dict[str, Any] = field(default_factory=dict)
    cost_cny: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    stage_metrics: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)


def _enhancement_fingerprint(
    *,
    section_id: str,
    packet_path: Path,
    old_draft_path: Path,
    application_settings: Mapping[str, Any],
    s2_metadata_fallback: Mapping[str, Any],
) -> dict[str, Any]:
    from optomind_research.chapter_asset_enhancer import (
        ENHANCEMENT_CONTRACT_VERSION,
        SCHEMA_VERSION as enhancer_schema_version,
        enhancement_contract_fingerprint,
    )

    return {
        "schema_version": "publication_mainline.enhancement_reuse.v2",
        "section_id": section_id,
        "input_packet_sha256": _sha256_file(packet_path),
        "old_draft_sha256": _sha256_file(old_draft_path),
        "enhancer_schema_version": str(enhancer_schema_version),
        "enhancer_contract_version": str(ENHANCEMENT_CONTRACT_VERSION),
        "enhancer_prompt_fingerprint": enhancement_contract_fingerprint(),
        "application_settings": {
            **dict(application_settings),
        },
        "s2_metadata_fallback": {
            **dict(s2_metadata_fallback),
        },
        "adapter_schema_version": SCHEMA_VERSION,
    }


def _build_owned_s2_metadata_callback(
    *,
    per_target_cap: int,
) -> Any:
    """Build a lazy S2 metadata/abstract search callback (no network).

    Construction only loads keys and instantiates the existing
    SemanticScholarBackend/router; the enhancer invokes the returned callback
    only when local candidates are unusable. ``max_results`` is defensively
    capped to the per-target allowance and at most six. Only metadata,
    abstracts, and snippets are requested; no full text is downloaded.
    """

    from tools.academic_backends.semantic_scholar_backend import (
        SemanticScholarBackend,
    )

    backend = SemanticScholarBackend()
    backend_lock = threading.RLock()
    cap = max(1, min(_S2_METADATA_FALLBACK_MAX_RESULTS, int(per_target_cap)))

    def callback(query: str, max_results: int) -> list[dict[str, Any]]:
        bounded = max(
            1,
            min(int(max_results or 0) or cap, _S2_METADATA_FALLBACK_MAX_RESULTS),
        )
        with backend_lock:
            return list(backend.search(query, max_results=bounded) or [])

    def close() -> None:
        closer = getattr(backend, "close", None)
        if callable(closer):
            with backend_lock:
                closer()

    callback.close = close  # type: ignore[attr-defined]
    return callback


def _resolve_application_settings(
    *,
    representative_applications_enabled: bool | None,
    application_max_targets: int | None,
    application_soft_min_targets: int | None,
    application_per_target_cap: int | None,
    application_local_max_results: int | None,
    application_writer_tier: str | None,
) -> tuple[dict[str, Any], list[str]]:
    """Resolve and normalize representative-application settings.

    ``None`` values fall back to the enhancer's own defaults. Local search
    results are clamped to at most six per request so the wiring never
    exceeds the intended restraint; the clamp is reported, never fatal.
    """

    from optomind_research.chapter_asset_enhancer import (
        DEFAULT_APPLICATION_LOCAL_MAX_RESULTS as ENH_LOCAL_MAX,
        DEFAULT_APPLICATION_MAX_TARGETS as ENH_MAX_TARGETS,
        DEFAULT_APPLICATION_PER_TARGET_CAP as ENH_PER_TARGET_CAP,
        DEFAULT_APPLICATION_SOFT_MIN_TARGETS as ENH_SOFT_MIN,
        DEFAULT_APPLICATION_WRITER_TIER as ENH_WRITER_TIER,
    )

    notes: list[str] = []
    enabled = (
        bool(representative_applications_enabled)
        if representative_applications_enabled is not None
        else DEFAULT_REPRESENTATIVE_APPLICATIONS_ENABLED
    )
    max_targets = max(
        1,
        int(
            application_max_targets
            if application_max_targets is not None
            else ENH_MAX_TARGETS
        ),
    )
    soft_min_targets = max(
        0,
        int(
            application_soft_min_targets
            if application_soft_min_targets is not None
            else ENH_SOFT_MIN
        ),
    )
    per_target_cap = int(
        application_per_target_cap
        if application_per_target_cap is not None
        else ENH_PER_TARGET_CAP
    )
    if per_target_cap > _APPLICATION_METADATA_UPPER_BOUND:
        notes.append(
            "application_per_target_cap_clamped_from_"
            f"{per_target_cap}_to_{_APPLICATION_METADATA_UPPER_BOUND}"
        )
        per_target_cap = _APPLICATION_METADATA_UPPER_BOUND
    per_target_cap = max(1, per_target_cap)
    local_max = int(
        application_local_max_results
        if application_local_max_results is not None
        else ENH_LOCAL_MAX
    )
    if local_max > _APPLICATION_METADATA_UPPER_BOUND:
        notes.append(
            f"application_local_max_results_clamped_from_{local_max}_to_{_APPLICATION_METADATA_UPPER_BOUND}"
        )
        local_max = _APPLICATION_METADATA_UPPER_BOUND
    local_max = max(1, local_max)
    writer_tier = str(
        application_writer_tier
        if application_writer_tier
        else ENH_WRITER_TIER
    )
    return (
        {
            "representative_applications_enabled": enabled,
            "application_max_targets": max_targets,
            "application_soft_min_targets": soft_min_targets,
            "application_per_target_cap": per_target_cap,
            "application_local_max_results": local_max,
            "application_writer_tier": writer_tier,
        },
        notes,
    )


def _write_enhancement_reuse_state(
    enhancer_dir: Path,
    fingerprint: Mapping[str, Any],
) -> None:
    enhancer_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        enhancer_dir / ENHANCEMENT_REUSE_STATE_JSON,
        {
            **dict(fingerprint),
            "updated_at": _now(),
        },
    )


def _enhancement_reuse_is_valid(
    *,
    enhancer_dir: Path,
    fingerprint: Mapping[str, Any],
) -> bool:
    enhanced_path = enhancer_dir / ENHANCED_CHAPTER_MD
    if not enhanced_path.is_file() or enhanced_path.stat().st_size == 0:
        return False
    report = _mapping(
        _read_json(enhancer_dir / ENHANCEMENT_REPORT_JSON)
    )
    if report.get("status") != "enhanced":
        return False
    if str(report.get("mode") or "") == "dry_run":
        return False
    state = _mapping(
        _read_json(enhancer_dir / ENHANCEMENT_REUSE_STATE_JSON)
    )
    required_keys = (
        "section_id",
        "input_packet_sha256",
        "old_draft_sha256",
        "enhancer_schema_version",
        "enhancer_contract_version",
        "enhancer_prompt_fingerprint",
        "application_settings",
        "s2_metadata_fallback",
        "adapter_schema_version",
    )
    for key in required_keys:
        if key in {
            "enhancer_prompt_fingerprint",
            "application_settings",
            "s2_metadata_fallback",
        }:
            if json.dumps(
                state.get(key) or {},
                sort_keys=True,
                ensure_ascii=False,
            ) != json.dumps(
                fingerprint.get(key) or {},
                sort_keys=True,
                ensure_ascii=False,
            ):
                return False
            continue
        if str(state.get(key) or "") != str(fingerprint.get(key) or ""):
            return False
    return True


class _TransientEnhancementFailure(Exception):
    def __init__(
        self,
        message: str,
        *,
        report: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.report = report


def _report_transport_errors(report: Mapping[str, Any]) -> list[str]:
    usage = _mapping(report.get("model_usage"))
    calls = usage.get("calls") or []
    if not isinstance(calls, list):
        return []
    errors: list[str] = []
    for call in calls:
        if isinstance(call, Mapping):
            error = str(call.get("error") or "").strip()
            if error:
                errors.append(error)
    return errors


def _is_transient_transport_failure(
    exc: BaseException | None,
    report: Mapping[str, Any] | None = None,
) -> bool:
    if isinstance(exc, _TransientEnhancementFailure):
        return True
    if exc is not None and type(exc).__name__ in _TRANSIENT_TRANSPORT_ERRORS:
        return True
    if report is not None:
        errors = _report_transport_errors(report)
        return bool(errors) and all(
            error in _TRANSIENT_TRANSPORT_ERRORS for error in errors
        )
    return False


def _run_one_enhancement_attempt(
    *,
    section_id: str,
    blueprint: Mapping[str, Any],
    section_blueprint: Mapping[str, Any],
    section_work_dir: Path,
    enhancer_dir: Path,
    old_draft_path: Path,
    packet_path: Path,
    project_root: Path,
    local_metadata_db_path: Path | None,
    enhancement_live: bool,
    local_search_callback: Any,
    s2_search_callback: Any,
    enhancement_qwen_caller: Any,
    runner: Callable[..., dict[str, Any]],
    retry_round: int,
    representative_applications_enabled: bool,
    application_max_targets: int,
    application_soft_min_targets: int,
    application_per_target_cap: int,
    application_local_max_results: int,
    application_writer_tier: str,
    s2_metadata_fallback: Mapping[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any], int]:
    if not old_draft_path.is_file() or old_draft_path.stat().st_size == 0:
        raise ValueError("R4 section draft is missing or empty")
    build_enhancer_input_packet(
        section_work_dir=section_work_dir,
        blueprint_section=section_blueprint,
        blueprint=dict(blueprint),
        output_path=packet_path,
        project_root=project_root,
        local_kb_path=local_metadata_db_path,
    )
    enhanced_path = enhancer_dir / ENHANCED_CHAPTER_MD
    reuse_fingerprint = _enhancement_fingerprint(
        section_id=section_id,
        packet_path=packet_path,
        old_draft_path=old_draft_path,
        application_settings={
            "representative_applications_enabled": (
                representative_applications_enabled
            ),
            "application_max_targets": application_max_targets,
            "application_soft_min_targets": application_soft_min_targets,
            "application_per_target_cap": application_per_target_cap,
            "application_local_max_results": application_local_max_results,
            "application_writer_tier": application_writer_tier,
        },
        s2_metadata_fallback=s2_metadata_fallback,
    )
    if enhancement_live and _enhancement_reuse_is_valid(
        enhancer_dir=enhancer_dir,
        fingerprint=reuse_fingerprint,
    ):
        persisted_usage = _mapping(
            _mapping(
                _read_json(enhancer_dir / ENHANCEMENT_REPORT_JSON)
            ).get("model_usage")
        )
        return (
            "reused",
            {
                "section_id": section_id,
                "enhanced_asset_dir": _relative(enhancer_dir, project_root),
                "authoritative_input_packet": _relative(
                    packet_path, project_root
                ),
                "source_old_draft": _relative(old_draft_path, project_root),
                "title": str(
                    section_blueprint.get("title")
                    or section_blueprint.get("section_title")
                    or section_id
                ),
                "reused": True,
            },
            dict(persisted_usage),
            retry_round,
        )

    enhancement_call_kwargs: dict[str, Any] = {
        "packet_path": packet_path,
        "old_draft_path": old_draft_path,
        "output_dir": enhancer_dir,
        "live": enhancement_live,
        "allow_overwrite": True,
        "representative_applications_enabled": (
            representative_applications_enabled
        ),
        "application_max_targets": application_max_targets,
        "application_soft_min_targets": application_soft_min_targets,
        "application_per_target_cap": application_per_target_cap,
        "application_local_max_results": application_local_max_results,
        "application_writer_tier": application_writer_tier,
    }
    if local_search_callback is not None:
        enhancement_call_kwargs["local_search_callback"] = local_search_callback
    if s2_search_callback is not None:
        enhancement_call_kwargs["s2_search_callback"] = s2_search_callback
    if enhancement_qwen_caller is not None:
        enhancement_call_kwargs["qwen_caller"] = enhancement_qwen_caller

    report = _call_enhancement_runner(
        runner,
        **enhancement_call_kwargs,
    )
    if str(report.get("mode") or "") == "dry_run":
        raise RuntimeError("enhancement runner was in dry-run mode")
    if str(report.get("status") or "") != "enhanced":
        if _is_transient_transport_failure(None, report=report):
            raise _TransientEnhancementFailure(
                "enhancement report was transport-fallback",
                report=report,
            )
        raise RuntimeError(
            f"enhancement status={report.get('status') or 'missing'}"
        )
    if not enhanced_path.is_file() or enhanced_path.stat().st_size == 0:
        raise RuntimeError("ENHANCED_CHAPTER.md missing or empty")
    _write_enhancement_reuse_state(enhancer_dir, reuse_fingerprint)
    return (
        "enhanced",
        {
            "section_id": section_id,
            "enhanced_asset_dir": _relative(enhancer_dir, project_root),
            "authoritative_input_packet": _relative(
                packet_path, project_root
            ),
            "source_old_draft": _relative(old_draft_path, project_root),
            "title": str(
                section_blueprint.get("title")
                or section_blueprint.get("section_title")
                or section_id
            ),
        },
        report.get("model_usage") or {},
        retry_round,
    )


def run_publication_mainline(
    *,
    project_root: str | Path,
    authoring_work_dir: str | Path,
    output_root: str | Path,
    admitted_section_ids: Sequence[str],
    blueprint: Mapping[str, Any],
    run_id: str = "",
    enhancement_live: bool = False,
    enhancement_qwen_caller: Any = None,
    enhancement_runner: Callable[..., dict[str, Any]] | None = None,
    enhancement_transient_retry_rounds: int = 2,
    enhancement_transient_retry_delay_seconds: float = 0.0,
    enhancement_workers: int = DEFAULT_ENHANCEMENT_WORKERS,
    local_metadata_db_path: str | Path | None = None,
    local_search_callback: Any = None,
    s2_search_callback: Any = None,
    representative_applications_enabled: bool | None = None,
    application_max_targets: int | None = None,
    application_soft_min_targets: int | None = None,
    application_per_target_cap: int | None = None,
    application_local_max_results: int | None = None,
    application_writer_tier: str | None = None,
    s2_metadata_fallback_enabled: bool | None = None,
    commander_live: bool = False,
    commander_model_tier: str = "c2_model",
    commander_role_provider: Any = None,
    staged_live: bool = False,
    staged_model_tier: str = "c_model",
    staged_reviewer_tier: str = "c2_model",
    staged_editorial_verifier_tier: str = "c2_model",
    staged_editorial_workers: int = 3,
    staged_reviewer_roles: Sequence[str] = (
        "continuity",
        "clarity",
        "reader_flow",
        "logic",
        "overlap",
    ),
    staged_providers: Mapping[str, Callable[..., Any]] | None = None,
    commander_resume: bool = False,
    staged_resume: bool = False,
) -> PublicationMainlineResult:
    """Run the established per-section publication mainline."""

    project_root = Path(project_root).resolve()
    authoring_work_dir = Path(authoring_work_dir)
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    enhancement_root = output_root / "enhancement"
    handoff_dir = output_root / "handoff"
    commander_dir = output_root / "commander"
    staged_context_dir = output_root / "staged_context"
    staged_work_dir = output_root / "staged_completion"
    downstream_dir = output_root / "staged_manuscript_sections"

    blueprint_sections = {
        str(section.get("section_id") or ""): section
        for section in blueprint.get("sections") or []
        if isinstance(section, Mapping) and section.get("section_id")
    }
    admitted = list(dict.fromkeys(str(value) for value in admitted_section_ids))

    failed_sections: list[dict[str, Any]] = []
    successful_sections: list[dict[str, Any]] = []
    enhancement_metrics: dict[str, Any] = {}
    total_cost_cny = 0.0
    total_input_tokens = 0
    total_output_tokens = 0

    runner = enhancement_runner or _default_enhancement_runner
    application_settings, application_settings_notes = (
        _resolve_application_settings(
            representative_applications_enabled=(
                representative_applications_enabled
            ),
            application_max_targets=application_max_targets,
            application_soft_min_targets=application_soft_min_targets,
            application_per_target_cap=application_per_target_cap,
            application_local_max_results=(
                application_local_max_results
            ),
            application_writer_tier=application_writer_tier,
        )
    )
    effective_s2_callback = s2_search_callback
    owned_s2_callback = None
    s2_fallback_notes: list[str] = []
    s2_fallback_enabled = (
        bool(s2_metadata_fallback_enabled)
        if s2_metadata_fallback_enabled is not None
        else DEFAULT_S2_METADATA_FALLBACK_ENABLED
    )
    s2_fallback_cap = max(
        1,
        min(
            _S2_METADATA_FALLBACK_MAX_RESULTS,
            int(application_settings["application_per_target_cap"]),
        ),
    )
    if s2_search_callback is not None:
        s2_callback_label = "injected"
    elif (
        not bool(application_settings["representative_applications_enabled"])
        or not s2_fallback_enabled
    ):
        s2_callback_label = "disabled"
    else:
        try:
            owned_s2_callback = _build_owned_s2_metadata_callback(
                per_target_cap=s2_fallback_cap
            )
            effective_s2_callback = owned_s2_callback
            s2_callback_label = "configured_default"
        except Exception as exc:  # noqa: BLE001 - fail open, never fatal
            effective_s2_callback = None
            s2_callback_label = "unavailable"
            s2_fallback_notes.append(
                f"s2_metadata_fallback_unavailable: {type(exc).__name__}"
            )
    s2_metadata_fallback = {
        "enabled": s2_fallback_enabled,
        "cap": s2_fallback_cap,
        "source": s2_callback_label,
    }
    owned_local_callback = None
    if local_search_callback is None and local_metadata_db_path:
        try:
            owned_local_callback = _LocalMetadataCallback(
                Path(local_metadata_db_path)
            )
        except Exception as exc:
            failed_sections.append(
                {
                    "section_id": "*",
                    "status": "local_metadata_callback_unavailable",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
    effective_local_callback = local_search_callback or owned_local_callback
    pending_sections = list(admitted)
    max_retry_rounds = max(0, int(enhancement_transient_retry_rounds))

    def run_section(
        section_id: str,
        retry_round: int,
    ) -> dict[str, Any]:
        """Enhance one section; never raises (fail-open per section)."""

        section_blueprint = blueprint_sections.get(section_id, {})
        section_work_dir = authoring_work_dir / "sections" / section_id
        enhancer_dir = enhancement_root / section_id
        old_draft_path = section_work_dir / "SECTION_DRAFT_EN.md"
        packet_path = enhancer_dir / "input_packet.json"
        try:
            attempt, section_record, usage, _ = _run_one_enhancement_attempt(
                section_id=section_id,
                blueprint=blueprint,
                section_blueprint=section_blueprint,
                section_work_dir=section_work_dir,
                enhancer_dir=enhancer_dir,
                old_draft_path=old_draft_path,
                packet_path=packet_path,
                project_root=project_root,
                local_metadata_db_path=(
                    Path(local_metadata_db_path)
                    if local_metadata_db_path
                    else None
                ),
                enhancement_live=enhancement_live,
                local_search_callback=effective_local_callback,
                s2_search_callback=effective_s2_callback,
                enhancement_qwen_caller=enhancement_qwen_caller,
                runner=runner,
                retry_round=retry_round,
                representative_applications_enabled=application_settings[
                    "representative_applications_enabled"
                ],
                application_max_targets=application_settings[
                    "application_max_targets"
                ],
                application_soft_min_targets=application_settings[
                    "application_soft_min_targets"
                ],
                application_per_target_cap=application_settings[
                    "application_per_target_cap"
                ],
                application_local_max_results=application_settings[
                    "application_local_max_results"
                ],
                application_writer_tier=application_settings[
                    "application_writer_tier"
                ],
                s2_metadata_fallback=s2_metadata_fallback,
            )
        except Exception as exc:  # noqa: BLE001 - fail-open per section
            return {
                "section_id": section_id,
                "ok": False,
                "transient": bool(
                    retry_round < max_retry_rounds
                    and _is_transient_transport_failure(exc)
                ),
                "error": exc,
            }
        return {
            "section_id": section_id,
            "ok": True,
            "attempt": attempt,
            "section_record": section_record,
            "usage": usage,
            "retry_round": retry_round,
        }

    try:
        for retry_round in range(max_retry_rounds + 1):
            next_pending: list[str] = []
            workers = max(
                1,
                min(int(enhancement_workers), max(1, len(pending_sections))),
            )
            if workers == 1:
                results = [
                    run_section(section_id, retry_round)
                    for section_id in pending_sections
                ]
            else:
                with ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="pub-mainline-enhance",
                ) as pool:
                    futures = {
                        pool.submit(run_section, section_id, retry_round): (
                            section_id
                        )
                        for section_id in pending_sections
                    }
                    by_id = {
                        futures[future]: future.result()
                        for future in as_completed(futures)
                    }
                results = [by_id[section_id] for section_id in pending_sections]
            for result in results:
                section_id = result["section_id"]
                if not result["ok"]:
                    exc = result["error"]
                    if result["transient"]:
                        next_pending.append(section_id)
                        continue
                    transport_errors = (
                        _report_transport_errors(exc.report)
                        if isinstance(exc, _TransientEnhancementFailure)
                        else []
                    )
                    failed_sections.append(
                        {
                            "section_id": section_id,
                            "status": "failed",
                            "reason": f"{type(exc).__name__}: {exc}",
                            "old_draft_path": str(
                                authoring_work_dir
                                / "sections"
                                / section_id
                                / "SECTION_DRAFT_EN.md"
                            ),
                            "enhancer_dir": str(
                                enhancement_root / section_id
                            ),
                            "retry_rounds": retry_round,
                            "transport_errors": transport_errors,
                        }
                    )
                    enhancement_metrics[section_id] = {
                        "status": "failed",
                        "retry_rounds": retry_round,
                        "transport_errors": transport_errors,
                    }
                    continue
                attempt = result["attempt"]
                section_record = result["section_record"]
                usage = result["usage"]
                enhancer_dir = enhancement_root / section_id
                if attempt == "reused":
                    enhancement_metrics[section_id] = {
                        "status": "enhanced",
                        "output_dir": str(enhancer_dir),
                        "reused": True,
                        "fingerprint_matched": True,
                        "retry_rounds": retry_round,
                    }
                else:
                    enhancement_metrics[section_id] = {
                        "status": "enhanced",
                        "output_dir": str(enhancer_dir),
                        "retry_rounds": retry_round,
                    }
                cost, cost_accounting = _usage_cost_accounting(
                    usage,
                    author_tier="c_model",
                    reviewer_tier="c2_model",
                    reused=attempt == "reused",
                )
                usage_input, usage_output = _sum_usage_tokens(usage)
                total_cost_cny += cost
                total_input_tokens += usage_input
                total_output_tokens += usage_output
                enhancement_metrics[section_id].update(
                    {
                        "cost_cny": round(cost, 6),
                        "input_tokens": usage_input,
                        "output_tokens": usage_output,
                        "cost_accounting": cost_accounting,
                    }
                )
                successful_sections.append(section_record)
            pending_sections = next_pending
            if (
                pending_sections
                and retry_round < max_retry_rounds
                and enhancement_transient_retry_delay_seconds > 0
            ):
                time.sleep(enhancement_transient_retry_delay_seconds)
    finally:
        if owned_s2_callback is not None:
            close = getattr(owned_s2_callback, "close", None)
            if callable(close):
                close()
        if owned_local_callback is not None:
            close = getattr(owned_local_callback, "close", None)
            if callable(close):
                close()

    enhancement_cost = round(total_cost_cny, 6)
    stage_metrics = {
        "publication_mainline_enhancement": {
            "status": (
                "completed" if successful_sections else "failed"
            ),
            "sections": enhancement_metrics,
            "application_settings": dict(application_settings),
            "application_settings_notes": list(application_settings_notes),
            "enhancement_workers": max(
                1, min(int(enhancement_workers), max(1, len(admitted)))
            ),
            "cost_cny": enhancement_cost,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "cost_accounting": (
                "provider_priced"
                if enhancement_cost > 0
                else (
                    "unaccounted_tokens"
                    if total_input_tokens or total_output_tokens
                    else "no_provider_usage"
                )
            ),
            "local_search_callback": (
                "injected"
                if local_search_callback is not None
                else (
                    "configured"
                    if owned_local_callback is not None
                    else "unavailable"
                )
            ),
            "s2_search_callback": s2_callback_label,
            "s2_metadata_fallback_cap": s2_fallback_cap,
            "s2_metadata_fallback_notes": list(s2_fallback_notes),
        },
        "publication_mainline_handoff": {
            "status": "pending",
            "cost_cny": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
        },
        "publication_mainline_commander": {
            "status": "pending",
            "cost_cny": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
        },
        "publication_mainline_staged_completion": {
            "status": "pending",
            "cost_cny": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
        },
    }
    if not successful_sections:
        return PublicationMainlineResult(
            status="failed",
            completed_stage="publication_mainline_enhancement",
            failed_sections=failed_sections,
            stage_metrics=stage_metrics,
            summary=_closed_summary(),
        )

    # Structure and quality are different concepts.  A failed enhancement must
    # remain in the plan so every downstream consumer can name the failed ID;
    # omitting it used to turn S09 into a chapter that appeared never to exist.
    enhanced_by_id = {
        str(row.get("section_id") or ""): dict(row)
        for row in successful_sections
    }
    failed_by_id = {
        str(row.get("section_id") or ""): dict(row)
        for row in failed_sections
        if str(row.get("section_id") or "") not in {"", "*"}
    }
    manifest_sections: list[dict[str, Any]] = []
    for section_id in admitted:
        if section_id in enhanced_by_id:
            manifest_sections.append(
                {**enhanced_by_id[section_id], "content_status": "enhanced"}
            )
            continue
        failure = failed_by_id.get(section_id, {})
        raw_draft = authoring_work_dir / "sections" / section_id / "SECTION_DRAFT_EN.md"
        if raw_draft.is_file() and raw_draft.stat().st_size:
            # ``_run_one_enhancement_attempt`` writes this packet before it
            # invokes the fallible enhancer.  Keep it with the fallback so the
            # Commander sees the original draft under the same evidence
            # boundary rather than an empty placeholder.
            fallback_packet = enhancement_root / section_id / "input_packet.json"
            manifest_sections.append(
                {
                    "section_id": section_id,
                    "title": str((blueprint_sections.get(section_id) or {}).get("title") or ""),
                    "content_status": "raw_fallback",
                    "source_old_draft": str(raw_draft),
                    "authoritative_input_packet": (
                        _relative(fallback_packet, project_root)
                        if fallback_packet.is_file()
                        and fallback_packet.stat().st_size
                        else ""
                    ),
                    "failure": failure,
                }
            )
        else:
            manifest_sections.append(
                {
                    "section_id": section_id,
                    "title": str((blueprint_sections.get(section_id) or {}).get("title") or ""),
                    "content_status": "explicitly_missing",
                    "failure": failure,
                }
            )

    manifest_path = output_root / "full_manuscript_manifest.json"
    _write_json(
        manifest_path,
        {
            "schema_version": "optomind.full_manuscript_handoff.manifest.v2",
            "project_root": str(project_root),
            "sections": manifest_sections,
        },
    )

    # These sub-stages run inside this adapter, past the reach of the
    # orchestrator's own start_stage/finish_stage timer, so each one measures
    # its own duration and reports it through stage_metrics.  Without this the
    # orchestrator's finish_stage finds no start timestamp and records 0.0:
    # on rhr_be780761 the commander billed 0.600954 CNY over 955,104 input
    # tokens and staged completion 0.661369 CNY over 525,857 tokens, both with
    # a wall time of zero in both ledgers.
    handoff_t0 = time.monotonic()
    try:
        from .full_manuscript_handoff import build_full_manuscript_handoff

        handoff_summary = build_full_manuscript_handoff(
            manifest_path=manifest_path,
            output_dir=handoff_dir,
        )
        handoff_path = handoff_dir / "UNIFIED_MANUSCRIPT_HANDOFF.json"
        if not handoff_path.is_file():
            raise RuntimeError("full manuscript handoff was not written")
        stage_metrics["publication_mainline_handoff"] = {
            "status": "completed",
            "cost_cny": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "wall_time_seconds": round(time.monotonic() - handoff_t0, 3),
            "summary": handoff_summary,
        }
    except Exception as exc:
        failed_sections.append(
            {
                "section_id": "*",
                "status": "handoff_failed",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        )
        return PublicationMainlineResult(
            status="failed",
            completed_stage="publication_mainline_handoff",
            enhanced_sections=[
                str(section["section_id"]) for section in successful_sections
            ],
            failed_sections=failed_sections,
            manifest_path=manifest_path,
            stage_metrics=stage_metrics,
            cost_cny=enhancement_cost,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            summary=_closed_summary(),
        )

    handoff_data = _mapping(_read_json(handoff_path))
    missing_sections = [
        row
        for row in manifest_sections
        if str(row.get("content_status") or "") == "explicitly_missing"
    ]
    raw_fallback_section_ids = [
        str(row.get("section_id") or "")
        for row in manifest_sections
        if str(row.get("content_status") or "") == "raw_fallback"
    ]
    if raw_fallback_section_ids:
        stage_metrics["publication_mainline_handoff"][
            "raw_fallback_section_ids"
        ] = raw_fallback_section_ids
    if missing_sections:
        # The handoff deliberately preserves the complete plan, but a staged
        # manuscript must not silently substitute a placeholder for a missing
        # chapter.  Stop before Commander while retaining the durable manifest
        # and handoff that identify exactly what needs recovery.
        stage_metrics["publication_mainline_handoff"]["missing_section_ids"] = [
            str(row.get("section_id") or "") for row in missing_sections
        ]
        return PublicationMainlineResult(
            status="failed",
            completed_stage="publication_mainline_handoff",
            enhanced_sections=[str(row["section_id"]) for row in successful_sections],
            failed_sections=failed_sections,
            manifest_path=manifest_path,
            handoff_path=handoff_path,
            stage_metrics=stage_metrics,
            cost_cny=enhancement_cost,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            summary=_closed_summary(),
        )
    commander_manifest_path = commander_dir / "commander_manifest.json"
    commander_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    _build_commander_manifest(
        project_root=project_root,
        handoff=handoff_data,
        output_path=commander_manifest_path,
    )

    commander_work_order_path = commander_dir / "global_commander_work_order.json"
    commander_t0 = time.monotonic()
    if (
        staged_resume
        and commander_work_order_path.is_file()
        and (commander_dir / "run_state.json").is_file()
    ):
        # Keep the previously persisted commander artifact byte-identical so
        # the staged completion run fingerprint is stable across resumes.
        commander_summary = {
            "status": str(
                _mapping(
                    _read_json(commander_dir / "run_state.json")
                ).get("status")
                or "failed"
            ),
            "resumed": True,
            "commander_reused_for_staged_resume": True,
        }
    else:
        try:
            from .global_manuscript_commander import (
                run_global_manuscript_commander,
            )

            commander_summary = run_global_manuscript_commander(
                manifest_path=commander_manifest_path,
                output_dir=commander_dir,
                model_tier=commander_model_tier,
                live=commander_live,
                resume=commander_resume,
                role_provider=commander_role_provider,
            )
        except Exception as exc:
            commander_summary = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
    if not isinstance(commander_summary, Mapping):
        commander_summary = {
            "status": "failed",
            "error": "Commander returned a non-mapping summary",
        }
    else:
        commander_summary = dict(commander_summary)
    commander_cost, commander_input, commander_output, commander_usage = (
        _commander_usage(commander_dir)
    )
    total_cost_cny += commander_cost
    total_input_tokens += commander_input
    total_output_tokens += commander_output
    commander_metric = {
        "status": str(commander_summary.get("status") or "failed"),
        "cost_cny": commander_cost,
        "input_tokens": commander_input,
        "output_tokens": commander_output,
        "wall_time_seconds": round(time.monotonic() - commander_t0, 3),
        "role_usage": commander_usage,
        "cost_accounting": (
            "provider_priced"
            if commander_cost > 0
            else (
                "unaccounted_tokens"
                if commander_input or commander_output
                else "no_provider_usage"
            )
        ),
    }
    if not commander_work_order_path.is_file():
        commander_error = str(
            commander_summary.get("error")
            or "Commander did not persist global_commander_work_order.json"
        )
        try:
            _build_commander_fail_open_work_order(
                handoff=handoff_data,
                output_path=commander_work_order_path,
                error=commander_error,
            )
            commander_summary.update(
                {
                    "status": "failed",
                    "fallback_used": True,
                    "fallback_mode": "handoff_section_order_only",
                    "fallback_work_order_path": str(commander_work_order_path),
                }
            )
            commander_metric.update(
                {
                    "status": "failed",
                    "fallback_used": True,
                    "fallback_mode": "handoff_section_order_only",
                }
            )
        except Exception as exc:
            commander_summary.update(
                {
                    "status": "failed",
                    "fallback_used": False,
                    "fallback_error": f"{type(exc).__name__}: {exc}",
                }
            )
            commander_metric["fallback_error"] = commander_summary[
                "fallback_error"
            ]
            stage_metrics["publication_mainline_commander"] = commander_metric
            return PublicationMainlineResult(
                status="failed",
                completed_stage="publication_mainline_commander",
                enhanced_sections=[
                    str(section["section_id"])
                    for section in successful_sections
                ],
                failed_sections=failed_sections,
                manifest_path=manifest_path,
                handoff_path=handoff_path,
                commander_manifest_path=commander_manifest_path,
                commander_summary=commander_summary,
                stage_metrics=stage_metrics,
                cost_cny=round(total_cost_cny, 6),
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                summary=_closed_summary(),
            )
    stage_metrics["publication_mainline_commander"] = commander_metric

    try:
        from .staged_manuscript_context import build_staged_manuscript_context

        context_summary = build_staged_manuscript_context(
            project_root=project_root,
            handoff_path=handoff_path,
            commander_work_order_path=commander_work_order_path,
            output_dir=staged_context_dir,
        )
    except Exception as exc:
        return PublicationMainlineResult(
            status="partial",
            completed_stage="publication_mainline_handoff",
            enhanced_sections=[
                str(section["section_id"]) for section in successful_sections
            ],
            failed_sections=failed_sections,
            manifest_path=manifest_path,
            handoff_path=handoff_path,
            commander_manifest_path=commander_manifest_path,
            commander_work_order_path=commander_work_order_path,
            commander_summary=commander_summary,
            fail_open_issues=[f"staged_context_failed: {exc}"],
            stage_metrics=stage_metrics,
            cost_cny=round(total_cost_cny, 6),
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            summary=_closed_summary(),
        )

    global_inputs = _mapping(
        _read_json(staged_context_dir / "STAGED_GLOBAL_INPUTS.json")
    )
    stage_inputs_payload = _mapping(
        _read_json(staged_context_dir / "STAGED_STAGE_INPUTS.json")
    )
    stage_inputs = {
        str(stage): _mapping(value)
        for stage, value in (stage_inputs_payload.get("stages") or {}).items()
        if isinstance(value, Mapping)
    }

    providers: dict[str, Callable[..., Any]] = dict(staged_providers or {})
    if staged_live and not staged_providers:
        from .staged_article_completion import (
            make_editorial_revision_qwen_provider,
            make_multi_reviewer_qwen_provider,
            make_qwen_stage_provider,
        )

        for stage in ("conclusion", "introduction", "abstract", "bounded_patch_proposals"):
            providers[stage] = make_qwen_stage_provider(
                stage,
                model_tier=staged_model_tier,
            )
        providers["whole_manuscript_review"] = make_multi_reviewer_qwen_provider(
            reviewers=[
                {"reviewer_id": role, "role": role}
                for role in staged_reviewer_roles
            ],
            model_tier=staged_reviewer_tier,
        )
        providers["editorial_revision"] = make_editorial_revision_qwen_provider(
            model_tier=staged_model_tier,
            verifier_tier=staged_editorial_verifier_tier,
            workers=staged_editorial_workers,
        )

        # Repair 3: Wire visual procurement into the live visual_remount stage.
        # The helper is fail-open: missing or rejected sources never block text.
        try:
            from .staged_article_completion import (
                visual_procurement_pre_step as _vp_pre_step,
            )
            _vp_staged_work_dir = staged_work_dir
            _vp_handoff_data = handoff_data
            _vp_local_kb = local_metadata_db_path

            def _visual_remount_live_provider(
                stage_inputs: Mapping[str, Any],
            ) -> dict[str, Any]:
                augmented = dict(stage_inputs)
                # Fix 4: build paper list from handoff sections sources when
                # stage_inputs carries no papers (the common case).
                if not augmented.get("papers"):
                    seen: set[str] = set()
                    hf_papers: list[dict[str, Any]] = []
                    central_by_title: dict[str, dict[str, Any]] = {}
                    if _vp_local_kb and Path(str(_vp_local_kb)).is_file():
                        try:
                            conn = sqlite3.connect(str(_vp_local_kb))
                            conn.row_factory = sqlite3.Row
                            for row in conn.execute(
                                "SELECT paper_id, doi, title, year, venue, "
                                "raw_json FROM papers WHERE title IS NOT NULL"
                            ):
                                key = re.sub(
                                    r"[^a-z0-9]+",
                                    " ",
                                    str(row["title"] or "").casefold(),
                                ).strip()
                                if key and key not in central_by_title:
                                    central_by_title[key] = dict(row)
                            conn.close()
                        except Exception:
                            central_by_title = {}
                    raw_sections = _vp_handoff_data.get("sections") or []
                    section_rows = (
                        raw_sections.values()
                        if isinstance(raw_sections, Mapping)
                        else raw_sections
                    )
                    for section in section_rows:
                        if not isinstance(section, Mapping):
                            continue
                        source_rows = list(section.get("sources") or [])
                        packet_ref = section.get("authoritative_input_packet")
                        packet_path = (
                            packet_ref.get("path")
                            if isinstance(packet_ref, Mapping)
                            else ""
                        )
                        if packet_path and Path(str(packet_path)).is_file():
                            packet = _read_json(Path(str(packet_path)))
                            coverage = packet.get("literature_coverage") or {}
                            if isinstance(coverage, Mapping):
                                source_rows.extend(coverage.get("sources") or [])
                        for src in source_rows:
                            if not isinstance(src, Mapping):
                                continue
                            pid = str(src.get("paper_id") or "")
                            if pid and pid not in seen:
                                seen.add(pid)
                                enriched = dict(src)
                                title_key = re.sub(
                                    r"[^a-z0-9]+",
                                    " ",
                                    str(src.get("title") or "").casefold(),
                                ).strip()
                                central = central_by_title.get(title_key)
                                if central:
                                    enriched["paper_id"] = str(
                                        central.get("paper_id") or pid
                                    )
                                    for key in ("doi", "year", "venue"):
                                        if not enriched.get(key) and central.get(key):
                                            enriched[key] = central[key]
                                hf_papers.append(enriched)
                    if hf_papers:
                        augmented["papers"] = hf_papers
                if not augmented.get("kb_sqlite") and _vp_local_kb:
                    augmented["kb_sqlite"] = str(_vp_local_kb)
                _vpm = _vp_pre_step(
                    augmented, work_dir=_vp_staged_work_dir
                )
                return {
                    "visual_procurement_manifest": _vpm,
                    "visual_package_path": str(
                        stage_inputs.get("final_visual_package") or ""
                    ),
                    "work_orders": [],
                    "status": (
                        "procurement_complete"
                        if str(_vpm.get("status") or "") not in {
                            "skip", "import_error", "offline_noop",
                            "no_papers_in_inputs",
                        }
                        else "noop"
                    ),
                }

            providers["visual_remount"] = _visual_remount_live_provider
        except ImportError:
            pass

    inputs = dict(global_inputs)
    inputs.setdefault("full_manuscript_handoff", handoff_data)
    inputs.setdefault("full_manuscript_handoff_path", str(handoff_path))
    inputs.setdefault(
        "commander_work_order",
        _mapping(_read_json(commander_work_order_path)),
    )
    inputs.setdefault("commander_work_order_path", str(commander_work_order_path))

    staged_t0 = time.monotonic()
    staged_resume_fallback = False
    try:
        from .staged_article_completion import (
            run_staged_article_completion,
        )

        def _run_staged_completion(*, resume: bool) -> Any:
            return run_staged_article_completion(
                work_dir=staged_work_dir,
                inputs=inputs,
                stage_inputs=stage_inputs,
                metadata={
                    "project_root": str(project_root),
                    "run_id": run_id,
                },
                stage_providers=providers,
                resume=resume,
                run_id=run_id,
                execution_context={
                    "work_dir": str(staged_work_dir),
                    "resume": resume,
                },
            )

        try:
            staged_state = _run_staged_completion(resume=staged_resume)
        except Exception as exc:
            # Rebuilding the handoff/context can legitimately change volatile
            # envelope fields while leaving the durable manuscript inputs
            # usable.  A strict staged fingerprint refusal must therefore be
            # recoverable: retry the staged boundary from scratch, preserving
            # all upstream assets and charging the same live global budget.
            # Other staged failures remain fail-closed and are surfaced below.
            if staged_resume and "fingerprint changed" in str(exc).lower():
                staged_resume_fallback = True
                staged_state = _run_staged_completion(resume=False)
            else:
                raise
    except Exception as exc:
        return PublicationMainlineResult(
            status="partial",
            completed_stage="publication_mainline_handoff",
            enhanced_sections=[
                str(section["section_id"]) for section in successful_sections
            ],
            failed_sections=failed_sections,
            manifest_path=manifest_path,
            handoff_path=handoff_path,
            commander_manifest_path=commander_manifest_path,
            commander_work_order_path=commander_work_order_path,
            commander_summary=commander_summary,
            staged_context_dir=staged_context_dir,
            fail_open_issues=[f"staged_completion_failed: {exc}"],
            stage_metrics=stage_metrics,
            cost_cny=round(total_cost_cny, 6),
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            summary=_closed_summary(),
        )

    (
        staged_input_tokens,
        staged_output_tokens,
        staged_cost,
        staged_usage_by_stage,
    ) = (
        _staged_usage(
            staged_state,
            author_tier=staged_model_tier,
            reviewer_tier=staged_reviewer_tier,
        )
    )
    total_input_tokens += staged_input_tokens
    total_output_tokens += staged_output_tokens
    total_cost_cny += staged_cost
    staged_accounting_labels = {
        str(metric.get("cost_accounting") or "")
        for metric in staged_usage_by_stage.values()
        if isinstance(metric, Mapping)
    }
    if "unaccounted_tokens" in staged_accounting_labels:
        staged_cost_accounting = "unaccounted_tokens"
    elif "estimated_from_tokens" in staged_accounting_labels:
        staged_cost_accounting = "estimated_from_tokens"
    elif "provider_priced" in staged_accounting_labels:
        staged_cost_accounting = "provider_priced"
    else:
        staged_cost_accounting = "no_provider_usage"
    stage_metrics["publication_mainline_staged_completion"] = {
        "status": str(staged_state.status),
        "cost_cny": staged_cost,
        "input_tokens": staged_input_tokens,
        "output_tokens": staged_output_tokens,
        "wall_time_seconds": round(time.monotonic() - staged_t0, 3),
        "stage_usage": staged_usage_by_stage,
        "cost_accounting": staged_cost_accounting,
        "resume_fallback": staged_resume_fallback,
    }

    final_review_path = staged_work_dir / "STAGED_COMPLETE_REVIEW_EN.md"
    if not final_review_path.is_file() or final_review_path.stat().st_size == 0:
        return PublicationMainlineResult(
            status="failed",
            completed_stage="publication_mainline_staged_completion",
            enhanced_sections=[
                str(section["section_id"]) for section in successful_sections
            ],
            failed_sections=failed_sections,
            manifest_path=manifest_path,
            handoff_path=handoff_path,
            commander_manifest_path=commander_manifest_path,
            commander_work_order_path=commander_work_order_path,
            commander_summary=commander_summary,
            staged_context_dir=staged_context_dir,
            staged_state_path=staged_work_dir / "staged_article_completion_state.json",
            staged_state=staged_state,
            stage_metrics=stage_metrics,
            cost_cny=round(total_cost_cny, 6),
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            summary=_closed_summary(
                delivery_gate_path=str(staged_work_dir / "STAGED_COMPLETE_REVIEW_EN.md"),
            ),
        )

    downstream_review_work_dir = _build_downstream_section_dir(
        staged_work_dir=staged_work_dir,
        staged_state=staged_state,
        output_dir=downstream_dir,
    )
    accounting = _editorial_accounting(staged_state)
    commander_failed = str(commander_summary.get("status") or "") != "completed"
    awaiting = list(getattr(staged_state, "awaiting_approval_stages", []) or [])
    fail_open_issues: list[str] = []
    if failed_sections:
        fail_open_issues.append(
            f"{len(failed_sections)} admitted section(s) not successfully enhanced"
        )
    if commander_failed:
        fail_open_issues.append(
            "commander final synthesis did not complete; work order retained"
        )
    if awaiting:
        fail_open_issues.append(
            "staged completion contains approval-required stage(s): "
            + ", ".join(awaiting)
        )
    if accounting["blocking_unresolved"]:
        fail_open_issues.append("critical unresolved editorial findings remain")
    if not accounting["closure_completed"]:
        fail_open_issues.append("editorial/quality closure is not completed")

    unaccounted_cost_stages = [
        stage_name
        for stage_name, metric in stage_metrics.items()
        if str(metric.get("cost_accounting") or "") == "unaccounted_tokens"
    ]

    if not final_review_path:
        status = "failed"
    elif awaiting or accounting["blocking_unresolved"]:
        status = "awaiting_human_review"
    elif (
        commander_failed
        or failed_sections
        or not accounting["closure_completed"]
        or str(staged_state.status) not in {"completed", "noop"}
    ):
        status = "partial"
    else:
        status = "completed"

    # Repair 4 / Fix E1: Derive the canonical article title via LLM when
    # staged_live is True (production mode), retrying up to 3 times before
    # falling back to the deterministic candidate set.  This prevents the raw
    # user question from ever appearing verbatim as the PDF title.
    # Fail-open: keep "" on any import/parse error so the rest of the
    # summary is never blocked.
    _article_title = ""
    try:
        from .staged_article_completion import (  # local import — avoid circular
            extract_presentation_ir,
            plan_review_titles,
        )
        _title_ir = extract_presentation_ir(
            {"full_manuscript_handoff": handoff_data}
            if isinstance(handoff_data, Mapping)
            else {}
        )
        if _title_ir is None:
            _title_ir = extract_presentation_ir(
                {"presentation_ir": global_inputs.get("presentation_ir")}
            )
        if _title_ir is not None:
            _llm_title_candidates: list[dict[str, Any]] | None = None
            if staged_live:
                # Attempt LLM-generated title candidates; fall through to the
                # deterministic fallback if every attempt fails.
                _llm_title_candidates = _generate_title_candidates_via_llm(
                    ir=_title_ir,
                    model_tier=staged_model_tier,
                    max_attempts=3,
                )
            _article_title = plan_review_titles(
                _title_ir,
                candidates_from_provider=_llm_title_candidates,
            ).selected_title
    except Exception:
        pass

    # Fix 5: Write article_title into a standalone content-package artifact so
    # downstream LaTeX/metadata pipelines can consume it without parsing the full
    # mainline summary.  Fail-open: a write failure never blocks the result.
    _metadata_path: Path | None = None
    try:
        _metadata_path = staged_work_dir / "PUBLICATION_METADATA.json"
        _write_json(
            _metadata_path,
            {
                "schema_version": "optomind.publication_metadata.v1",
                # "title" is the canonical key consumed by _normalize_metadata in the
                # LaTeX renderer (resolve_publication_metadata → raw.get("title")).
                # "article_title" is kept for backwards-compatibility with any code
                # that reads this artifact directly.
                "title": _article_title,
                "article_title": _article_title,
                # The manuscript is produced by the OptoMind research
                # harness.  Use the stable corporate author label when no
                # human author metadata was supplied; an empty author field
                # must not leak the renderer's placeholder into a delivered
                # PDF.
                "authors": [{"name": "OptoMind"}],
                "created_at": _now(),
            },
        )
    except Exception:
        _metadata_path = None

    completed_stage = "publication_mainline_staged_completion"
    result = PublicationMainlineResult(
        status=status,
        completed_stage=completed_stage,
        enhanced_sections=[
            str(section["section_id"]) for section in successful_sections
        ],
        failed_sections=failed_sections,
        manifest_path=manifest_path,
        handoff_path=handoff_path,
        commander_manifest_path=commander_manifest_path,
        commander_work_order_path=commander_work_order_path,
        commander_summary=commander_summary,
        staged_context_dir=staged_context_dir,
        staged_state_path=staged_work_dir / "staged_article_completion_state.json",
        staged_state=staged_state,
        final_review_path=final_review_path,
        downstream_review_work_dir=downstream_review_work_dir,
        editorial_closure_completed=accounting["closure_completed"],
        fail_open_issues=fail_open_issues,
        accounting=accounting,
        cost_cny=round(total_cost_cny, 6),
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        stage_metrics=stage_metrics,
        summary={
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "completed_stage": completed_stage,
            "section_count": len(admitted),
            "enhanced_section_count": len(successful_sections),
            "failed_section_count": len(failed_sections),
            "artifacts": {
                "manifest": str(manifest_path),
                "handoff": str(handoff_path),
                "commander_manifest": str(commander_manifest_path),
                "commander_work_order": str(commander_work_order_path),
                "staged_context_dir": str(staged_context_dir),
                "staged_state": str(
                    staged_work_dir / "staged_article_completion_state.json"
                ),
                "final_review": str(final_review_path),
                "downstream_review_work_dir": str(downstream_review_work_dir),
            },
            "cost_cny": round(total_cost_cny, 6),
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "cost_accounting": (
                "provider_priced"
                if total_cost_cny > 0
                else (
                    "unaccounted_tokens"
                    if total_input_tokens or total_output_tokens
                    else "no_provider_usage"
                )
            ),
            "unaccounted_cost_stages": unaccounted_cost_stages,
            "fail_open_issues": fail_open_issues,
            "editorial_accounting": accounting,
            # Repair 2: explicit delivery gate independent of non-blocking
            # issues so callers can distinguish a valid final artifact from a
            # truly missing one without inspecting fail_open_issues.
            # Repair 4: resolved title; never the raw user question verbatim.
            "article_title": _article_title,
            "delivery_gate": (
                "open"
                if (
                    final_review_path
                    and final_review_path.is_file()
                    and final_review_path.stat().st_size > 0
                )
                else "closed"
            ),
            "delivery_gate_path": str(final_review_path) if final_review_path else "",
            "metadata_path": str(_metadata_path) if _metadata_path else "",
            "created_at": _now(),
        },
    )
    summary_path = output_root / "PUBLICATION_MAINLINE_SUMMARY.json"
    _write_json(summary_path, result.summary)
    return result


__all__ = [
    "SCHEMA_VERSION",
    "PublicationMainlineResult",
    "build_enhancer_input_packet",
    "run_publication_mainline",
]
