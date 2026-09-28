"""Standalone task-specific A/B reading cards for one paper.

The builder owns one paper, one immutable material snapshot, and one M1
research plan.  It makes a single topic-aware Qwen request that returns two
logically independent views:

* ``general_understanding`` (A), a readable scientific introduction; and
* ``review_planning`` (B), self-contained review-specific material.

The two views share a call and therefore share provenance.  The runtime never
claims that A is topic-independent or safe to reuse as a general cache.  This
module does not build a review outline, planner, evidence DAG, or citation
graph.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .local_materials import PreparedSnapshot, PreparedSnapshotProvider
from .module4.runtime import (
    GlobalBudgetLedger,
    QwenDirectClient,
    QwenTransportError,
    estimated_cost_cny,
    invoke_client,
)
from .paper_reading_card_economy import build_economy_messages
from .paper_reading_card_prompt import PROMPT_VERSION, build_messages


SCHEMA_VERSION = "optomind.paper_reading_card.v1"
MODEL = "qwen3.7-flash"
INPUT_PROFILES = {"standard", "economy"}
DEFAULT_INPUT_PROFILE = "economy"
DEFAULT_THINKING = True
DEFAULT_THINKING_BUDGET = 512
DEFAULT_CONCISE_OUTPUT = False
_STOP_REASONS = {"stop", "end_turn", "completed", "complete"}
_ROLE_LABELS = {
    "background",
    "context",
    "contribution",
    "direct evidence",
    "direct_evidence",
    "finding",
    "method",
    "mechanism",
    "review",
    "theory",
}
_PLACEHOLDER_RE = re.compile(
    r"^(?:n/?a|none|unknown|not available|not stated|placeholder|tbd|todo|"
    r"should (?:retrieve|verify|check|investigate)|to be determined)\.?$",
    re.IGNORECASE,
)


class PaperReadingCardError(ValueError):
    """Base error for deterministic card input/output failures."""


class CardInputError(PaperReadingCardError):
    pass


class CardOutputError(PaperReadingCardError):
    pass


class CardTransportError(PaperReadingCardError):
    pass


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _norm(value: Any) -> str:
    if isinstance(value, Mapping) or (isinstance(value, Sequence) and not isinstance(value, (str, bytes))):
        return ""
    return re.sub(r"\s+", " ", _text(value)).strip()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _read_json(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CardInputError(f"invalid_json_input:{target.name}") from exc
    if not isinstance(value, Mapping):
        raise CardInputError(f"json_object_required:{target.name}")
    return dict(value)


def _load_plan(plan_path: str | Path) -> dict[str, Any]:
    """Load the M1 wrapper and return only the plan scope used by this card."""

    raw = _read_json(plan_path)
    plan = raw.get("plan") if isinstance(raw.get("plan"), Mapping) else raw
    if not isinstance(plan, Mapping):
        raise CardInputError("m1_plan_object_required")
    question = _norm(plan.get("question_en") or plan.get("question") or plan.get("research_question"))
    research_object = _norm(plan.get("research_object"))
    if not question and not research_object:
        raise CardInputError("m1_plan_question_or_research_object_required")
    raw_facets = plan.get("facets")
    if raw_facets is None:
        raw_facets = plan.get("roles") or []
    if not isinstance(raw_facets, Sequence) or isinstance(raw_facets, (str, bytes)):
        raise CardInputError("m1_plan_facets_must_be_array")
    facets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_facets):
        if not isinstance(item, Mapping):
            raise CardInputError(f"m1_plan_facet_not_object:{index}")
        facet_id = _norm(item.get("id") or item.get("facet_id"))
        if not facet_id:
            raise CardInputError(f"m1_plan_facet_id_missing:{index}")
        if facet_id in seen:
            raise CardInputError(f"m1_plan_facet_id_duplicate:{facet_id}")
        seen.add(facet_id)
        facets.append(
            {
                "id": facet_id,
                "ask": _norm(item.get("ask") or item.get("question") or item.get("description")),
                "keyword_queries": [
                    _norm(value) for value in (item.get("keyword_queries") or ()) if _norm(value)
                ],
                "question_queries": [
                    _norm(value) for value in (item.get("question_queries") or ()) if _norm(value)
                ],
                "filters": dict(item.get("filters") or {}) if isinstance(item.get("filters"), Mapping) else {},
                "must_exclude": [_norm(value) for value in (item.get("must_exclude") or ()) if _norm(value)],
            }
        )
    criteria = dict(plan.get("criteria") or {}) if isinstance(plan.get("criteria"), Mapping) else {}
    return {
        "schema_version": _text(plan.get("schema_version") or "research_harness.query_plan.v2"),
        "question_en": question,
        "research_object": research_object,
        "facets": facets,
        "criteria": criteria,
        "additional_constraints": list(plan.get("additional_constraints") or ()) if isinstance(plan.get("additional_constraints") or (), Sequence) and not isinstance(plan.get("additional_constraints"), (str, bytes)) else [],
    }


def load_plan(plan_path: str | Path) -> dict[str, Any]:
    """Public plan loader used by the CLI and tests."""

    return _load_plan(plan_path)


def _snapshot_payload(snapshot: PreparedSnapshot, packet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "manifest": dict(snapshot.manifest),
        "blocks": [dict(row) for row in snapshot.blocks],
        "assets": [dict(row) for row in snapshot.assets],
        "references": [dict(row) for row in snapshot.references],
        "reading_packet": dict(packet),
    }


def _paper_identity(packet: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    identity = dict(packet.get("paper_identity") or {})
    identity.setdefault("canonical_paper_id", _text(manifest.get("canonical_paper_id")))
    identity.setdefault("snapshot_id", _text(manifest.get("snapshot_id")))
    for key in ("title", "doi", "year", "publication_version"):
        identity[key] = _text(identity.get(key))
    return identity


def build_card_input(snapshot: PreparedSnapshot, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Build the complete, non-truncated input packet for one card."""

    if not isinstance(snapshot, PreparedSnapshot):
        raise CardInputError("prepared_snapshot_required")
    if not isinstance(plan, Mapping):
        raise CardInputError("m1_plan_required")
    packet = snapshot.build_reading_packet()
    if not packet.get("eligible"):
        raise CardInputError("reading_material_not_eligible")
    if not packet.get("observations"):
        raise CardInputError("reading_material_empty")
    identity = _paper_identity(packet, snapshot.manifest)
    normalized_plan = _normalize_plan_mapping(plan)
    snapshot_hash = _sha(_snapshot_payload(snapshot, packet))
    topic_hash = _sha(normalized_plan)
    model_packet = _compact_model_packet(packet)
    material = {
        "snapshot_id": _text(packet.get("snapshot_id")),
        "snapshot_sha256": snapshot_hash,
        "material_scope": _text(packet.get("material_scope")),
        "declared_content_depth": _text(packet.get("declared_content_depth")),
        "identity_status": _text(packet.get("identity_status")),
        "known_gaps": [dict(item) for item in packet.get("known_gaps") or () if isinstance(item, Mapping)],
        "observation_count": len(packet.get("observations") or ()),
        "usage_limits": dict(packet.get("usage_limits") or {}),
    }
    card_id = "paper-card-" + _sha({"paper_identity": identity, "snapshot_sha256": snapshot_hash, "topic_sha256": topic_hash})[:24]
    return {
        "schema_version": SCHEMA_VERSION,
        "card_id": card_id,
        "paper_identity": identity,
        "material": material,
        "review_plan": normalized_plan,
        "snapshot_id": _text(packet.get("snapshot_id")),
        "snapshot_sha256": snapshot_hash,
        "topic_sha256": topic_hash,
        "reading_packet": packet,
        "model_reading_packet": model_packet,
    }


def _compact_model_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Remove repeated provenance metadata while retaining every source word.

    The complete packet remains in ``INPUT_PACKET.json`` and its hash covers
    it.  The model sees all observations without the per-observation copy of
    paper identity, source paths, and raw hashes, which can dominate a large
    paper's context window.  No text is clipped or selected here.
    """

    observations: list[dict[str, Any]] = []
    for item in packet.get("observations") or ():
        if not isinstance(item, Mapping):
            continue
        source = item.get("source") if isinstance(item.get("source"), Mapping) else {}
        observations.append(
            {
                "observation_id": _text(item.get("observation_id")),
                "source_kind": _text(item.get("source_kind") or source.get("source_kind")),
                "section_path": list(source.get("section_path") or ()) if isinstance(source.get("section_path"), Sequence) and not isinstance(source.get("section_path"), (str, bytes)) else [],
                "text": _text(item.get("text")),
                "source": {
                    "snapshot_id": _text(source.get("snapshot_id") or packet.get("snapshot_id")),
                    "block_id": _text(source.get("block_id")),
                    "source_document_id": _text(source.get("source_document_id")),
                    "source_kind": _text(source.get("source_kind") or item.get("source_kind")),
                },
            }
        )
    return {
        "schema_version": _text(packet.get("schema_version")),
        "snapshot_id": _text(packet.get("snapshot_id")),
        "material_scope": _text(packet.get("material_scope")),
        "declared_content_depth": _text(packet.get("declared_content_depth")),
        "eligible": bool(packet.get("eligible")),
        "identity_status": _text(packet.get("identity_status")),
        "known_gaps": [dict(item) for item in packet.get("known_gaps") or () if isinstance(item, Mapping)],
        "usage_limits": dict(packet.get("usage_limits") or {}),
        "observations": observations,
    }


def _normalize_plan_mapping(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize caller-provided plans without re-reading or adding scope."""

    question = _norm(plan.get("question_en") or plan.get("question") or plan.get("research_question"))
    research_object = _norm(plan.get("research_object"))
    raw_facets = plan.get("facets") or plan.get("roles") or []
    if not question and not research_object:
        raise CardInputError("m1_plan_question_or_research_object_required")
    if not isinstance(raw_facets, Sequence) or isinstance(raw_facets, (str, bytes)):
        raise CardInputError("m1_plan_facets_must_be_array")
    facets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_facets):
        if not isinstance(item, Mapping):
            raise CardInputError(f"m1_plan_facet_not_object:{index}")
        facet_id = _norm(item.get("id") or item.get("facet_id"))
        if not facet_id:
            raise CardInputError(f"m1_plan_facet_id_missing:{index}")
        if facet_id in seen:
            raise CardInputError(f"m1_plan_facet_id_duplicate:{facet_id}")
        seen.add(facet_id)
        facets.append(
            {
                "id": facet_id,
                "ask": _norm(item.get("ask") or item.get("question") or item.get("description")),
                "keyword_queries": [_norm(v) for v in (item.get("keyword_queries") or ()) if _norm(v)],
                "question_queries": [_norm(v) for v in (item.get("question_queries") or ()) if _norm(v)],
                "filters": dict(item.get("filters") or {}) if isinstance(item.get("filters"), Mapping) else {},
                "must_exclude": [_norm(v) for v in (item.get("must_exclude") or ()) if _norm(v)],
            }
        )
    criteria = dict(plan.get("criteria") or {}) if isinstance(plan.get("criteria"), Mapping) else {}
    return {
        "schema_version": _text(plan.get("schema_version") or "research_harness.query_plan.v2"),
        "question_en": question,
        "research_object": research_object,
        "facets": facets,
        "criteria": criteria,
        "additional_constraints": list(plan.get("additional_constraints") or ()) if isinstance(plan.get("additional_constraints") or (), Sequence) and not isinstance(plan.get("additional_constraints"), (str, bytes)) else [],
    }


def _normalize_input_profile(value: Any) -> str:
    profile = _text(DEFAULT_INPUT_PROFILE if value is None else value).strip().casefold()
    if profile not in INPUT_PROFILES:
        raise CardInputError("input_profile_must_be_standard_or_economy")
    return profile


def _build_standard_messages(card_input: Mapping[str, Any]) -> list[dict[str, str]]:
    prompt_input = dict(card_input)
    prompt_input["reading_packet"] = dict(card_input.get("model_reading_packet") or card_input.get("reading_packet") or {})
    return build_messages(prompt_input)


def _build_card_messages(
    card_input: Mapping[str, Any],
    *,
    input_profile: str = "standard",
    concise_output: bool = False,
) -> list[dict[str, str]]:
    profile = _normalize_input_profile(input_profile)
    if concise_output and profile != "economy":
        raise CardInputError("concise_output_requires_economy_profile")
    if profile == "economy":
        messages, _ = build_economy_messages(card_input, concise_output=concise_output)
        return messages
    return _build_standard_messages(card_input)


def _build_card_request(
    card_input: Mapping[str, Any],
    *,
    input_profile: str = DEFAULT_INPUT_PROFILE,
    concise_output: bool = DEFAULT_CONCISE_OUTPUT,
) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    """Build the exact live request and optional economy audit."""

    profile = _normalize_input_profile(input_profile)
    if concise_output and profile != "economy":
        raise CardInputError("concise_output_requires_economy_profile")
    messages = build_card_messages(
        card_input,
        input_profile=profile,
        concise_output=concise_output,
    )
    if profile == "economy":
        _, audit = build_economy_messages(card_input, concise_output=concise_output)
        return messages, audit
    return messages, None


def build_card_messages(
    card_input: Mapping[str, Any],
    *,
    input_profile: str = DEFAULT_INPUT_PROFILE,
    concise_output: bool = DEFAULT_CONCISE_OUTPUT,
) -> list[dict[str, str]]:
    """Public deterministic message builder for standard or economy input."""

    return _build_card_messages(
        card_input,
        input_profile=input_profile,
        concise_output=concise_output,
    )


def _decode_content(result: Any) -> dict[str, Any]:
    value = result
    if isinstance(result, Mapping) and "content" in result:
        value = result.get("content")
    if isinstance(value, Mapping):
        return dict(value)
    text = _text(value).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CardOutputError("response_not_valid_json") from exc
    if not isinstance(decoded, Mapping):
        raise CardOutputError("response_json_object_required")
    return dict(decoded)


def _meaningful(value: Any) -> bool:
    if isinstance(value, str):
        text = _norm(value)
        # Short scientific terms, especially in Chinese, can be informative.
        # Length cannot certify meaning; content quality is reviewed separately.
        return bool(text) and not _PLACEHOLDER_RE.fullmatch(text) and text.casefold() not in _ROLE_LABELS
    if isinstance(value, Mapping):
        return any(_meaningful(child) for key, child in value.items() if _text(key).casefold() not in {"facet_id", "id"})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_meaningful(child) for child in value)
    return False


def _normalize_findings(value: Any, field: str) -> list[dict[str, str]]:
    if value is None or (isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and not value):
        return []
    rows = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else [value]
    normalized: list[dict[str, str]] = []
    for index, row in enumerate(rows):
        if isinstance(row, Mapping):
            listing_value = row.get("listing")
            listing_text = _norm(listing_value) if isinstance(listing_value, str) else ""
            if "finding" not in row and listing_text:
                finding_value = row.get("listing")
            else:
                finding_value = row.get("finding") or row.get("claim") or row.get("text") or row.get("summary")
            if "finding" in row and "listing" in row:
                finding_value_canonical = _norm(row.get("finding"))
                finding_value_alias = listing_text
                if finding_value_canonical and finding_value_alias and finding_value_canonical != finding_value_alias:
                    raise CardOutputError("key_findings_finding_alias_conflict")
            finding = _norm(finding_value)
            conditions = _norm(row.get("conditions") or row.get("condition") or row.get("context"))
        else:
            finding = _norm(row)
            conditions = ""
        if not finding:
            raise CardOutputError(f"{field}_item_empty:{index}")
        normalized.append({"finding": finding, "conditions": conditions})
    return normalized


def _normalize_contributions(value: Any) -> list[dict[str, str]]:
    if value is None or (isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and not value):
        return []
    rows = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else [value]
    normalized: list[dict[str, str]] = []
    for index, row in enumerate(rows):
        if isinstance(row, Mapping):
            contribution = _norm(row.get("contribution") or row.get("summary") or row.get("text"))
            limits = _norm(row.get("limits") or row.get("limitations") or row.get("boundary"))
        else:
            contribution = _norm(row)
            limits = ""
        if not contribution:
            raise CardOutputError(f"contribution_item_empty:{index}")
        normalized.append({"contribution": contribution, "limits": limits})
    return normalized


def _normalize_facet_contributions(value: Any, known_ids: set[str]) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise CardOutputError("facet_contributions_must_be_array")
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise CardOutputError(f"facet_contribution_not_object:{index}")
        facet_id = _norm(row.get("facet_id") or row.get("id"))
        if not facet_id or facet_id not in known_ids:
            raise CardOutputError(f"facet_id_unknown:{facet_id or index}")
        # A paper can supply several distinct contributions to one Facet.
        # This is a contribution list, not a unique-key Facet dictionary.
        contribution = _norm(row.get("contribution") or row.get("summary") or row.get("text"))
        possible_uses = _norm(row.get("possible_uses") or row.get("uses") or row.get("review_use"))
        boundaries = _norm(row.get("boundaries") or row.get("boundary") or row.get("limitations"))
        if not _meaningful(contribution) or not _meaningful(possible_uses):
            raise CardOutputError(f"facet_contribution_content_missing:{facet_id}")
        rows.append({"facet_id": facet_id, "contribution": contribution, "possible_uses": possible_uses, "boundaries": boundaries})
    return rows


def _normalize_broader_uses(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise CardOutputError("broader_review_uses_must_be_array")
    rows: list[dict[str, str]] = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise CardOutputError(f"broader_review_use_not_object:{index}")
        purpose = _norm(row.get("purpose") or row.get("role"))
        material = _norm(row.get("material") or row.get("content") or row.get("text"))
        connection = _norm(row.get("connection_to_review") or row.get("connection") or row.get("why_useful"))
        if not (_meaningful(purpose) and _meaningful(material) and _meaningful(connection)):
            raise CardOutputError(f"broader_review_use_content_missing:{index}")
        rows.append({"purpose": purpose, "material": material, "connection_to_review": connection})
    return rows


def _all_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [_norm(value)]
    if isinstance(value, Mapping):
        return [text for child in value.values() for text in _all_text(child)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [text for child in value for text in _all_text(child)]
    return []


def validate_card_output(result: Any, card_input: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize and validate A/B content against known plan IDs and depth."""

    raw = _decode_content(result)
    a = raw.get("general_understanding")
    b = raw.get("review_planning")
    if not isinstance(a, Mapping) or not isinstance(b, Mapping):
        raise CardOutputError("general_understanding_and_review_planning_required")
    # One observed response used the truncated key ``appro``.  Preserve the
    # original response and accept this single alias only when the canonical
    # key is absent and the alias contains a non-empty string.  If both keys
    # carry different content, fail closed instead of silently choosing one.
    a = dict(a)
    if "approach" not in a and "appro" in a:
        alias_value = _norm(a.get("appro")) if isinstance(a.get("appro"), str) else ""
        if alias_value:
            a["approach"] = alias_value
    elif "approach" in a and "appro" in a:
        approach_value = _norm(a.get("approach"))
        alias_value = _norm(a.get("appro")) if isinstance(a.get("appro"), str) else ""
        if approach_value and alias_value and approach_value != alias_value:
            raise CardOutputError("general_understanding_approach_alias_conflict")
    required_a = ("paper_kind", "research_scope", "work_summary", "problem_or_question", "approach", "key_findings", "contribution_and_limits")
    missing_a = [field for field in required_a if field not in a]
    if missing_a:
        raise CardOutputError("general_understanding_fields_missing:" + ",".join(missing_a))
    normalized_a = {
        "paper_kind": _norm(a.get("paper_kind")),
        "research_scope": _norm(a.get("research_scope")),
        "work_summary": _norm(a.get("work_summary")),
        "problem_or_question": _norm(a.get("problem_or_question")),
        "approach": _norm(a.get("approach")),
        "key_findings": _normalize_findings(a.get("key_findings"), "key_findings"),
        "contribution_and_limits": _normalize_contributions(a.get("contribution_and_limits")),
    }
    for field in ("paper_kind", "research_scope", "work_summary", "problem_or_question", "approach"):
        if field == "paper_kind":
            kind = normalized_a[field].casefold()
            allowed_kinds = ("empirical", "theory", "methods", "review", "perspective", "other", "实证", "理论", "方法", "综述", "观点", "其他")
            if not kind or not any(kind == item or kind.startswith(item + " ") for item in allowed_kinds):
                raise CardOutputError(f"general_understanding_field_empty:{field}")
            continue
        if not _meaningful(normalized_a[field]):
            raise CardOutputError(f"general_understanding_field_empty:{field}")
    # A short abstract or a narrowly scoped methods note may not state a
    # finding or contribution.  Empty arrays are valid in that case; the
    # surrounding A fields and the scope cautions carry the uncertainty.

    required_b = ("planning_summary", "topic_handles")
    missing_b = [field for field in required_b if field not in b]
    if missing_b:
        raise CardOutputError("review_planning_fields_missing:" + ",".join(missing_b))
    planning_summary = _norm(b.get("planning_summary"))
    if not _meaningful(planning_summary):
        raise CardOutputError("planning_summary_empty")
    raw_handles = b.get("topic_handles")
    if not isinstance(raw_handles, Sequence) or isinstance(raw_handles, (str, bytes)):
        raise CardOutputError("topic_handles_must_be_array")
    topic_handles = [_norm(v) for v in raw_handles if _norm(v)]
    if not topic_handles or not all(_meaningful(v) for v in topic_handles):
        raise CardOutputError("topic_handles_empty_or_generic")
    known_ids = {_text(item.get("id")) for item in (card_input.get("review_plan") or {}).get("facets") or () if isinstance(item, Mapping)}
    facet_contributions = _normalize_facet_contributions(b.get("facet_contributions", []), known_ids)
    broader = _normalize_broader_uses(b.get("broader_review_uses", []))
    cautions_raw = b.get("scope_interpretation_cautions", [])
    if not isinstance(cautions_raw, Sequence) or isinstance(cautions_raw, (str, bytes)):
        raise CardOutputError("scope_interpretation_cautions_must_be_array")
    cautions = [_norm(v) for v in cautions_raw if _norm(v)]
    normalized_b = {
        "planning_summary": planning_summary,
        "topic_handles": topic_handles,
        "facet_contributions": facet_contributions,
        "broader_review_uses": broader,
        "scope_interpretation_cautions": cautions,
    }
    material_scope = _text((card_input.get("material") or {}).get("material_scope"))
    if not _meaningful(normalized_a) or not _meaningful(normalized_b):
        raise CardOutputError("a_or_b_meaningful_content_missing")
    return {
        "general_understanding": normalized_a,
        "review_planning": normalized_b,
    }


def _extract_telemetry(result: Mapping[str, Any]) -> dict[str, Any]:
    usage = result.get("usage") if isinstance(result.get("usage"), Mapping) else {}
    return {
        "model": _text(result.get("returned_model") or result.get("requested_model") or MODEL),
        "requested_model": _text(result.get("requested_model") or MODEL),
        "returned_model": _text(result.get("returned_model") or ""),
        "finish_reason": _text(result.get("finish_reason") or ""),
        "complete": bool(result.get("complete", True)),
        "usage": dict(usage),
        "raw_response_sha256": _text(result.get("raw_response_sha256") or ""),
        "request_id": _text(result.get("request_id") or ""),
        "call_id": _text(result.get("call_id") or ""),
        "elapsed_seconds": result.get("elapsed_seconds"),
    }


def _check_transport_result(result: Mapping[str, Any]) -> None:
    finish_reason = _text(result.get("finish_reason") or "stop").casefold()
    if result.get("truncated") or result.get("partial_stream") or result.get("overrun"):
        raise CardTransportError("qwen_response_truncated_or_overrun")
    if finish_reason not in _STOP_REASONS or result.get("complete") is False:
        raise CardTransportError("qwen_incomplete_response")
    if not (result.get("content") or (result.get("general_understanding") and result.get("review_planning"))):
        raise CardTransportError("qwen_empty_response")


def render_card_markdown(card: Mapping[str, Any]) -> str:
    """Render only validated card data; never call a model."""

    identity = card.get("paper_identity") or {}
    material = card.get("material") or {}
    a = card.get("general_understanding") or {}
    b = card.get("review_planning") or {}
    lines = [
        "# Paper Reading Card",
        "",
        f"- Card: `{_text(card.get('card_id'))}`",
        f"- Paper: `{_text(identity.get('title'))}`",
        f"- Canonical paper ID: `{_text(identity.get('canonical_paper_id'))}`",
        f"- Material scope: `{_text(material.get('material_scope'))}`",
        f"- Declared content depth: `{_text(material.get('declared_content_depth'))}`",
        f"- Snapshot: `{_text(card.get('snapshot_id'))}`",
        "",
        "## A. General understanding",
        "",
        f"**Paper kind:** {_text(a.get('paper_kind'))}",
        f"\n**Research scope:** {_text(a.get('research_scope'))}",
        f"\n**Work summary:** {_text(a.get('work_summary'))}",
        f"\n**Problem or question:** {_text(a.get('problem_or_question'))}",
        f"\n**Approach:** {_text(a.get('approach'))}",
        "",
        "### Key findings",
        "",
    ]
    for row in a.get("key_findings") or ():
        lines.append(f"- {_text(row.get('finding'))}" + (f" (Conditions: {_text(row.get('conditions'))})" if row.get("conditions") else ""))
    lines.extend(["", "### Contribution and limits", ""])
    for row in a.get("contribution_and_limits") or ():
        lines.append(f"- {_text(row.get('contribution'))}" + (f" Limits: {_text(row.get('limits'))}" if row.get("limits") else ""))
    lines.extend(["", "## B. Review planning", "", f"{_text(b.get('planning_summary'))}", "", "### Topic handles", ""])
    lines.extend(f"- {value}" for value in b.get("topic_handles") or ())
    if b.get("facet_contributions"):
        lines.extend(["", "### Facet contributions", ""])
        for row in b.get("facet_contributions") or ():
            lines.append(f"- **{_text(row.get('facet_id'))}:** {_text(row.get('contribution'))} Uses: {_text(row.get('possible_uses'))}. Boundaries: {_text(row.get('boundaries'))}")
    if b.get("broader_review_uses"):
        lines.extend(["", "### Broader review uses", ""])
        for row in b.get("broader_review_uses") or ():
            lines.append(f"- **{_text(row.get('purpose'))}:** {_text(row.get('material'))} Connection: {_text(row.get('connection_to_review'))}")
    lines.extend(["", "### Scope and interpretation cautions", ""])
    lines.extend(f"- {value}" for value in b.get("scope_interpretation_cautions") or ())
    lines.extend(["", "## Provenance", "", "A and B were produced in one topic-aware joint model call; A is not claimed to be topic-independent or cache-reusable.", ""])
    return "\n".join(lines)


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _existing_success(output_dir: Path, card_input: Mapping[str, Any], *, expected: Mapping[str, Any]) -> dict[str, Any] | None:
    card_path = output_dir / "PAPER_READING_CARD.json"
    commit_path = output_dir / "COMMIT.json"
    if not card_path.is_file() or not commit_path.is_file():
        return None
    try:
        card = _read_json(card_path)
        commit = _read_json(commit_path)
    except CardInputError:
        return None
    if commit.get("status") != "success":
        return None
    if commit.get("card_id") != card_input.get("card_id") or commit.get("snapshot_sha256") != card_input.get("snapshot_sha256") or commit.get("topic_sha256") != card_input.get("topic_sha256"):
        raise CardInputError("output_exists_for_different_card_input")
    for key, value in expected.items():
        if commit.get(key) != value:
            raise CardInputError("output_exists_with_different_runtime_or_prompt")
    for name, digest in (commit.get("files") or {}).items():
        path = output_dir / _text(name)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != _text(digest):
            raise CardInputError("output_commit_hash_mismatch")
    if card.get("schema_version") != SCHEMA_VERSION:
        raise CardInputError("output_card_schema_mismatch")
    return {"card": card, "reused": True, "output_dir": str(output_dir)}


def _ensure_output_dir(output_dir: Path, card_input: Mapping[str, Any], *, expected: Mapping[str, Any]) -> dict[str, Any] | None:
    if not output_dir.exists():
        output_dir.mkdir(parents=True, exist_ok=False)
        return None
    existing = _existing_success(output_dir, card_input, expected=expected)
    if existing is not None:
        return existing
    if any(output_dir.iterdir()):
        raise CardInputError("output_exists_use_new_directory")
    return None


def _estimate_reservation(messages: Sequence[Mapping[str, Any]], *, output_tokens: int, thinking_budget: int) -> float:
    request_bytes = json.dumps({"model": MODEL, "messages": list(messages)}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    input_tokens = len(request_bytes) + 8192 + (256 * max(1, len(messages)))
    return estimated_cost_cny({"prompt_tokens": input_tokens, "completion_tokens": output_tokens + thinking_budget}, model=MODEL, conservative=True)


def run_paper_reading_card(
    *,
    snapshot_dir: str | Path,
    plan_path: str | Path,
    output_dir: str | Path,
    key_file: str | Path | None = None,
    budget_ledger_path: str | Path | None = None,
    budget_limit_cny: float | None = None,
    client: Any | None = None,
    model: str = MODEL,
    input_profile: str = DEFAULT_INPUT_PROFILE,
    thinking: bool = DEFAULT_THINKING,
    concise_output: bool = DEFAULT_CONCISE_OUTPUT,
    max_output_tokens: int = 8192,
    thinking_budget: int = DEFAULT_THINKING_BUDGET,
    timeout_seconds: float = 300.0,
    max_retries: int = 0,
    call_id: str = "",
) -> dict[str, Any]:
    """Run one card, persist immutable artifacts, and return the card."""

    if model != MODEL:
        raise CardInputError("paper_reading_card_model_must_be_qwen3_7_flash")
    profile = _normalize_input_profile(input_profile)
    if concise_output and profile != "economy":
        raise CardInputError("concise_output_requires_economy_profile")
    effective_thinking_budget = int(thinking_budget) if thinking else 0
    if client is None:
        if not key_file:
            raise CardInputError("live_run_key_file_required")
        if not budget_ledger_path:
            raise CardInputError("live_run_shared_budget_ledger_required")
        if budget_limit_cny is None or not math.isfinite(float(budget_limit_cny)) or float(budget_limit_cny) <= 0:
            raise CardInputError("live_run_finite_positive_budget_required")
    snapshot = PreparedSnapshotProvider(snapshot_dir).load()
    plan = _load_plan(plan_path)
    card_input = build_card_input(snapshot, plan)
    output = Path(output_dir)
    messages, input_audit = _build_card_request(
        card_input,
        input_profile=profile,
        concise_output=concise_output,
    )
    runtime_config = {
        "input_profile": profile,
        "thinking": bool(thinking),
        "concise_output": bool(concise_output),
        "max_output_tokens": int(max_output_tokens),
        "thinking_budget": effective_thinking_budget,
        "timeout_seconds": float(timeout_seconds),
        "max_retries": int(max_retries),
    }
    if input_audit is not None:
        input_audit = dict(input_audit)
        input_audit["runtime_config"] = dict(runtime_config)
    input_audit_sha256 = _sha(input_audit) if input_audit is not None else ""
    expected_commit = {
        "prompt_sha256": _sha(messages),
        "model": MODEL,
        "runtime_config": runtime_config,
        "input_audit_sha256": input_audit_sha256,
    }
    reused = _ensure_output_dir(output, card_input, expected=expected_commit)
    if reused is not None:
        return reused
    input_artifact = {
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "card_id": card_input["card_id"],
        "paper_identity": card_input["paper_identity"],
        "material": card_input["material"],
        "review_plan": card_input["review_plan"],
        "snapshot_id": card_input["snapshot_id"],
        "snapshot_sha256": card_input["snapshot_sha256"],
        "topic_sha256": card_input["topic_sha256"],
        "reading_packet": card_input["reading_packet"],
        "model_reading_packet": card_input["model_reading_packet"],
        "runtime_config": runtime_config,
    }
    _atomic_json(output / "INPUT_PACKET.json", input_artifact)
    _atomic_json(
        output / "PROMPT.json",
        {
            "prompt_version": PROMPT_VERSION,
            "messages": messages,
            "prompt_sha256": _sha(messages),
            "input_profile": profile,
            "concise_output": bool(concise_output),
            "thinking": bool(thinking),
        },
    )
    if input_audit is not None:
        _atomic_json(output / "INPUT_AUDIT.json", input_audit)
    ledger = GlobalBudgetLedger(limit_cny=budget_limit_cny, path=budget_ledger_path) if budget_ledger_path else None
    if client is None:
        client = QwenDirectClient(
            model=MODEL,
            key_file=key_file,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            max_retries=max(0, int(max_retries)),
            thinking=bool(thinking),
            thinking_budget=effective_thinking_budget,
            json_mode=True,
            raw_response_dir=output / "raw_responses",
            budget_ledger=ledger,
        )
    call_name = call_id or f"paper-card:{card_input['card_id']}"
    try:
        raw_result = invoke_client(
            client,
            messages,
            model=MODEL,
            max_output_tokens=max_output_tokens,
            thinking=bool(thinking),
            thinking_budget=effective_thinking_budget,
            call_id=call_name,
        )
    except QwenTransportError as exc:
        _atomic_json(output / "FAILURE.json", {"status": "failed", "phase": "transport", "error": type(exc).__name__, "message": str(exc), "record": dict(exc.record), "model": MODEL, "prompt_sha256": _sha(messages)})
        raise
    except Exception as exc:
        _atomic_json(output / "FAILURE.json", {"status": "failed", "phase": "transport", "error": type(exc).__name__, "message": str(exc), "model": MODEL, "prompt_sha256": _sha(messages)})
        raise CardTransportError(f"paper_card_client_failed:{type(exc).__name__}") from exc
    if not isinstance(raw_result, Mapping):
        raw_result = {"content": raw_result, "complete": True, "finish_reason": "stop"}
    raw_result = dict(raw_result)
    _atomic_json(output / "RAW_RESPONSE.json", raw_result)
    _atomic_json(output / "USAGE.json", _extract_telemetry(raw_result))
    try:
        _check_transport_result(raw_result)
        normalized = validate_card_output(raw_result, card_input)
    except (CardTransportError, CardOutputError) as exc:
        _atomic_json(output / "FAILURE.json", {"status": "failed", "phase": "validation", "error": type(exc).__name__, "message": str(exc), "model": MODEL, "prompt_sha256": _sha(messages), "telemetry": _extract_telemetry(raw_result)})
        raise
    material = dict(card_input["material"])
    card = {
        "schema_version": SCHEMA_VERSION,
        "card_id": card_input["card_id"],
        "paper_identity": dict(card_input["paper_identity"]),
        "material": material,
        "task": {
            "plan_schema_version": _text((card_input.get("review_plan") or {}).get("schema_version")),
            "question_en": _text((card_input.get("review_plan") or {}).get("question_en")),
            "research_object": _text((card_input.get("review_plan") or {}).get("research_object")),
            "topic_sha256": card_input["topic_sha256"],
        },
        "snapshot_id": card_input["snapshot_id"],
        "snapshot_sha256": card_input["snapshot_sha256"],
        "topic_sha256": card_input["topic_sha256"],
        "general_understanding": normalized["general_understanding"],
        "review_planning": normalized["review_planning"],
        "planning_view": {
            "paper_identity": dict(card_input["paper_identity"]),
            "material_scope": material.get("material_scope"),
            "declared_content_depth": material.get("declared_content_depth"),
            "snapshot_id": card_input["snapshot_id"],
            "topic_sha256": card_input["topic_sha256"],
            "planning_summary": normalized["review_planning"]["planning_summary"],
            "topic_handles": normalized["review_planning"]["topic_handles"],
            "facet_contributions": normalized["review_planning"]["facet_contributions"],
            "broader_review_uses": normalized["review_planning"]["broader_review_uses"],
            "scope_interpretation_cautions": normalized["review_planning"]["scope_interpretation_cautions"],
        },
        "output_provenance": {
            "joint_topic_aware_call": True,
            "general_understanding_topic_independent": False,
            "general_understanding_cache_reuse": "not_claimed",
            "model": MODEL,
            "prompt_version": PROMPT_VERSION,
            "prompt_sha256": _sha(messages),
            "input_profile": profile,
            "thinking": bool(thinking),
            "concise_output": bool(concise_output),
            "usage": _extract_telemetry(raw_result),
        },
    }
    _atomic_json(output / "PAPER_READING_CARD.json", card)
    (output / "PAPER_READING_CARD.md").write_text(render_card_markdown(card), encoding="utf-8", newline="\n")
    commit = {
        "schema_version": SCHEMA_VERSION,
        "status": "success",
        "card_id": card["card_id"],
        "snapshot_id": card["snapshot_id"],
        "snapshot_sha256": card["snapshot_sha256"],
        "topic_sha256": card["topic_sha256"],
        "prompt_sha256": _sha(messages),
        "input_audit_sha256": input_audit_sha256,
        "runtime_config": expected_commit["runtime_config"],
        "model": MODEL,
        "files": {},
    }
    file_names = [
        "INPUT_PACKET.json",
        "PROMPT.json",
        "RAW_RESPONSE.json",
        "USAGE.json",
        "PAPER_READING_CARD.json",
        "PAPER_READING_CARD.md",
    ]
    if input_audit is not None:
        file_names.insert(2, "INPUT_AUDIT.json")
    commit["files"] = {
        name: hashlib.sha256((output / name).read_bytes()).hexdigest()
        for name in file_names
    }
    _atomic_json(output / "COMMIT.json", commit)
    return {"card": card, "reused": False, "output_dir": str(output)}


def preflight_card(
    *,
    snapshot_dir: str | Path,
    plan_path: str | Path,
    input_profile: str = DEFAULT_INPUT_PROFILE,
    thinking: bool = DEFAULT_THINKING,
    concise_output: bool = DEFAULT_CONCISE_OUTPUT,
    max_output_tokens: int = 8192,
    thinking_budget: int = DEFAULT_THINKING_BUDGET,
    timeout_seconds: float = 300.0,
    max_retries: int = 0,
) -> dict[str, Any]:
    """Build the exact input and report sizing without calling a model."""

    profile = _normalize_input_profile(input_profile)
    if concise_output and profile != "economy":
        raise CardInputError("concise_output_requires_economy_profile")
    effective_thinking_budget = int(thinking_budget) if thinking else 0
    snapshot = PreparedSnapshotProvider(snapshot_dir).load()
    plan = _load_plan(plan_path)
    card_input = build_card_input(snapshot, plan)
    messages, input_audit = _build_card_request(
        card_input,
        input_profile=profile,
        concise_output=concise_output,
    )
    if input_audit is not None:
        input_audit = dict(input_audit)
        input_audit["runtime_config"] = {
            "input_profile": profile,
            "thinking": bool(thinking),
            "concise_output": bool(concise_output),
            "max_output_tokens": int(max_output_tokens),
            "thinking_budget": effective_thinking_budget,
            "timeout_seconds": float(timeout_seconds),
            "max_retries": int(max_retries),
        }
    request_bytes = json.dumps({"model": MODEL, "messages": messages}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema_version": SCHEMA_VERSION,
        "network_call": False,
        "model": MODEL,
        "card_id": card_input["card_id"],
        "snapshot_id": card_input["snapshot_id"],
        "snapshot_sha256": card_input["snapshot_sha256"],
        "topic_sha256": card_input["topic_sha256"],
        "paper_identity": card_input["paper_identity"],
        "material": card_input["material"],
        "facet_ids": [item["id"] for item in card_input["review_plan"]["facets"]],
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": _sha(messages),
        "input_profile": profile,
        "thinking": bool(thinking),
        "concise_output": bool(concise_output),
        "thinking_budget": effective_thinking_budget,
        "input_audit_sha256": _sha(input_audit) if input_audit is not None else "",
        "request_bytes": len(request_bytes),
        "estimated_reserved_cny": _estimate_reservation(messages, output_tokens=max_output_tokens, thinking_budget=effective_thinking_budget),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build one standalone A/B paper reading card.")
    parser.add_argument("--snapshot", required=True, help="Prepared immutable snapshot directory")
    parser.add_argument("--plan", required=True, help="M1 PLAN.json")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--key-file", default="")
    parser.add_argument("--budget-ledger", default="")
    parser.add_argument("--budget-limit-cny", type=float, default=None)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--thinking-budget", type=int, default=DEFAULT_THINKING_BUDGET)
    parser.add_argument("--input-profile", choices=sorted(INPUT_PROFILES), default=DEFAULT_INPUT_PROFILE)
    parser.add_argument("--no-thinking", action="store_true", default=not DEFAULT_THINKING, help="Disable model thinking and reserve no thinking tokens")
    parser.add_argument("--concise-output", action="store_true", default=DEFAULT_CONCISE_OUTPUT, help="Use economy output length guidance; requires --input-profile economy")
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--max-retries", type=int, default=0)
    parser.add_argument("--preflight", action="store_true", help="Default: inspect inputs without a model call")
    parser.add_argument("--run", action="store_true", help="Explicitly permit one live model call")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.preflight or not args.run:
            print(json.dumps(preflight_card(snapshot_dir=args.snapshot, plan_path=args.plan, input_profile=args.input_profile, thinking=not args.no_thinking, concise_output=args.concise_output, max_output_tokens=args.max_output_tokens, thinking_budget=args.thinking_budget, timeout_seconds=args.timeout_seconds, max_retries=args.max_retries), ensure_ascii=False, indent=2))
            return 0
        result = run_paper_reading_card(
            snapshot_dir=args.snapshot,
            plan_path=args.plan,
            output_dir=args.output_dir,
            key_file=args.key_file or None,
            budget_ledger_path=args.budget_ledger or None,
            budget_limit_cny=args.budget_limit_cny,
            input_profile=args.input_profile,
            thinking=not args.no_thinking,
            concise_output=args.concise_output,
            max_output_tokens=args.max_output_tokens,
            thinking_budget=args.thinking_budget,
            timeout_seconds=args.timeout_seconds,
            max_retries=args.max_retries,
        )
        print(json.dumps({"card_id": result["card"]["card_id"], "output_dir": result["output_dir"], "reused": result["reused"]}, ensure_ascii=False, indent=2))
        return 0
    except (PaperReadingCardError, QwenTransportError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=os.sys.stderr)
        return 2


__all__ = [
    "SCHEMA_VERSION",
    "MODEL",
    "INPUT_PROFILES",
    "DEFAULT_INPUT_PROFILE",
    "DEFAULT_THINKING",
    "DEFAULT_THINKING_BUDGET",
    "DEFAULT_CONCISE_OUTPUT",
    "PaperReadingCardError",
    "CardInputError",
    "CardOutputError",
    "CardTransportError",
    "load_plan",
    "build_card_input",
    "build_card_messages",
    "validate_card_output",
    "render_card_markdown",
    "run_paper_reading_card",
    "preflight_card",
]


if __name__ == "__main__":
    raise SystemExit(main())
