"""Extract traceable propositions and open-world scientific-axis candidates.

The extractor is generic: scientific axes are supplied by the current user
question or proposed from current material.  No topic-specific axis is
embedded in the prompt or validation logic.
"""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from llm.qwen_chat_client import call_qwen_chat

from .artifact_store import atomic_write_json


CARD_SCHEMA_VERSION = "optomind.material_proposition_card.v1"
RUN_SCHEMA_VERSION = "optomind.material_proposition_extraction.v1"
FORMAT_RETRY_MAX_TOKENS_DEFAULT = 8000
FORMAT_RETRY_INSTRUCTION = (
    "Your previous response was truncated or was not a valid JSON object. "
    "Output ONLY a compact valid JSON object with the essential semantic "
    "fields and table answers. No narration, no repetition, no markdown. "
    "Valid JSON object only."
)


class MaterialCardExtractionError(RuntimeError):
    """Final parse/validation failure carrying provider usage for audit."""

    def __init__(
        self,
        message: str,
        *,
        usage: Mapping[str, Any] | None = None,
        per_attempt_usage: Iterable[Mapping[str, Any]] | None = None,
        format_retry_count: int = 0,
    ) -> None:
        super().__init__(message)
        self.usage = dict(usage or {})
        self.per_attempt_usage = [
            dict(row) for row in (per_attempt_usage or [])
        ]
        self.format_retry_count = int(format_retry_count)

_SPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

_QUESTION_RELEVANCE = frozenset(
    {"central", "substantial", "contextual", "out_of_scope"}
)
_PAPER_FUNCTIONS = frozenset(
    {
        "theory_or_foundation",
        "method_or_model",
        "mechanism",
        "reported_result",
        "comparison_or_benchmark",
        "validation_or_translation",
        "limitation_or_boundary",
        "application_example",
        "review_synthesis",
        "background_context",
    }
)
_PROPOSITION_KINDS = frozenset(
    {
        "definition",
        "method",
        "mechanism",
        "finding",
        "comparison",
        "validation",
        "limitation",
        "boundary_condition",
        "research_gap",
        "background_context",
    }
)
_PROPOSITION_STANCES = frozenset(
    {"reports", "supports", "qualifies", "challenges", "compares"}
)
_QUESTION_FUNCTIONS = frozenset(
    {
        "direct_answer",
        "comparison_input",
        "mechanism_context",
        "validation_boundary",
        "method_context",
        "background_context",
        "gap_signal",
    }
)
_AXIS_LEVELS = frozenset(
    {"primary_axis_candidate", "subaxis_candidate", "cross_cutting_candidate"}
)
_CEILING_RANK = {
    "discovery_only": 0,
    "background_and_candidate_only": 1,
    "contextual_or_qualified_support": 2,
    "factual_support": 3,
}


_SYSTEM_PROMPT = """You extract structured scientific propositions from one paper packet for a literature-review system.

Use only the supplied evidence excerpts. Every scientific proposition and every material-emergent axis candidate must cite exact supplied chunk_ids. Do not invent a mechanism, number, causal claim, comparison, bibliographic identity, or experimental result.

Scientific axes are open-world. The packet contains seed axes derived from the current user's question. Assign a seed axis when it genuinely fits, but do not force every paper into every seed axis. If the paper exposes a scientifically important dimension that the seed axes do not adequately represent, propose it under emergent_axis_candidates. An emergent axis must be described generically from the evidence, explain why existing seed axes are insufficient, and cite its basis chunks. Do not propose a new axis merely to rename a seed axis or capture a one-sentence detail.

Evidence permission is fixed upstream. Abstract-only material may state what the authors report, but must not be promoted into stronger evidence. Treat the supplied evidence_ceiling values as hard ceilings.

The packet may include a supplementary_gap_context that names the specific gap
(claim evidence, section argument, review structure, whole review, or visual
need) that triggered retrieval, its coverage targets, and the broad search
background. Judge question_relevance against BOTH the supplied user_question
AND this supplementary_gap_context when present. A paper can be useful as
mechanism, boundary, validation, counterexample, or background for the named
gap even if it does not directly study the headline method. Only material
with no defensible contribution to either the overall question or the named
gap is out_of_scope.

Return exactly one JSON object with this shape and no Markdown:
{
  "question_relevance": "central|substantial|contextual|out_of_scope",
  "paper_functions": ["method_or_model"],
  "seed_axis_assignments": [
    {
      "axis_id": "Q01",
      "fit": "central|substantial|contextual",
      "question_function": "direct_answer|comparison_input|mechanism_context|validation_boundary|method_context|background_context|gap_signal",
      "reason": "",
      "basis_chunk_ids": []
    }
  ],
  "emergent_axis_candidates": [
    {
      "label": "",
      "definition": "",
      "why_seed_axes_are_insufficient": "",
      "relationship_to_question": "",
      "proposed_level": "primary_axis_candidate|subaxis_candidate|cross_cutting_candidate",
      "parent_seed_axis_ids": [],
      "basis_chunk_ids": []
    }
  ],
  "propositions": [
    {
      "statement": "",
      "proposition_kind": "definition|method|mechanism|finding|comparison|validation|limitation|boundary_condition|research_gap|background_context",
      "stance": "reports|supports|qualifies|challenges|compares",
      "question_function": "direct_answer|comparison_input|mechanism_context|validation_boundary|method_context|background_context|gap_signal",
      "explicitly_stated": true,
      "evidence_chunk_ids": []
    }
  ],
  "background_contexts": [
    {"statement": "", "basis_chunk_ids": []}
  ],
  "extraction_warnings": []
}
"""


def _text(value: Any, limit: int = 2000) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()[: max(0, int(limit))]


def _values(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return list(dict.fromkeys(_text(item, 240) for item in value if _text(item, 240)))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _supplementary_task_reference(
    packet: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Compact locally generated reference for the supplementary task.

    Only identity fields and a deterministic hash of the bounded context are
    persisted; the full context is never duplicated into every annotation.
    """

    context = packet.get("supplementary_context")
    if not isinstance(context, Mapping) or not context:
        return None
    coverage_ids = list(
        dict.fromkeys(
            str(entry.get("coverage_id") or "")
            for entry in (context.get("coverage_catalog") or [])
            if isinstance(entry, Mapping)
            and str(entry.get("coverage_id") or "")
        )
    )
    digest = hashlib.sha256(
        _canonical_json(context).encode("utf-8")
    ).hexdigest()
    return {
        "task_id": _text(context.get("task_id"), 240),
        "gap_type": _text(context.get("gap_type"), 120),
        "coverage_ids": coverage_ids,
        "context_sha256": "sha256:" + digest,
    }


def _parse_json_object(content: str) -> dict[str, Any]:
    value = json.loads(str(content or ""))
    if not isinstance(value, dict):
        raise ValueError("Material proposition response is not a JSON object")
    return value


def _merge_usage_rows(
    usage_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate provider usage across attempts without double counting."""
    rows = [
        dict(row) for row in usage_rows if isinstance(row, Mapping)
    ]
    first = rows[0] if rows else {}
    input_tokens = sum(int(row.get("input_tokens") or 0) for row in rows)
    output_tokens = sum(int(row.get("output_tokens") or 0) for row in rows)
    return {
        "module": first.get("module") or "MaterialPropositionExtractor",
        "agent_name": (
            first.get("agent_name") or "MaterialPropositionExtractor"
        ),
        "model_tier": first.get("model_tier") or "",
        "model_name": first.get("model_name") or "unknown",
        "task_type": first.get("task_type") or "research_chat",
        "mock_llm": any(bool(row.get("mock_llm")) for row in rows),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "model_call_count": len(rows),
        "format_retry_count": max(0, len(rows) - 1),
        "success": bool(rows) and all(
            bool(row.get("success")) for row in rows
        ),
        "token_usage_source": (
            "provider_response"
            if any(
                row.get("token_usage_source") == "provider_response"
                for row in rows
            )
            else "estimated"
        ),
        "per_attempt_usage": rows,
    }


def _normalized_label(value: Any) -> str:
    return _NON_ALNUM_RE.sub(" ", _text(value, 200).casefold()).strip()


def _seed_axis_catalog(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize current and legacy question-axis records."""

    raw_catalog = (
        packet.get("seed_axis_catalog")
        or packet.get("facet_catalog")
        or []
    )
    catalog: list[dict[str, Any]] = []
    for row in raw_catalog:
        if not isinstance(row, Mapping):
            continue
        axis_id = _text(row.get("axis_id") or row.get("facet_id"), 80)
        description = _text(
            row.get("description") or row.get("label"),
            1200,
        )
        if not axis_id or not description:
            continue
        normalized = dict(row)
        normalized["axis_id"] = axis_id
        normalized["description"] = description
        normalized.setdefault("origin", "user_question")
        normalized.setdefault("status", "seed")
        catalog.append(normalized)
    return catalog


def _stable_proposition_id(
    work_id: str,
    statement: str,
    evidence_ids: Iterable[str],
) -> str:
    source = "|".join(
        [work_id, _normalized_label(statement), *sorted(set(evidence_ids))]
    )
    return "prop:" + hashlib.sha1(source.encode("utf-8")).hexdigest()[:20]


def build_material_extraction_messages(
    packet: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Build a generic prompt; all scientific axes come from packet data."""

    # Older material-packet artifacts called this field ``facet_catalog``.
    # Accept that spelling at the extraction boundary so a valid, already
    # paid acquisition run can be resumed without silently dropping the
    # user's question axes.  New artifacts continue to use
    # ``seed_axis_catalog``.
    seed_axis_catalog = _seed_axis_catalog(packet)

    payload = {
        "canonical_work_id": packet.get("canonical_work_id"),
        "paper_identity": packet.get("canonical_identity"),
        "material_classes": packet.get("material_classes"),
        "user_question": packet.get("question"),
        "question_seed_axes": seed_axis_catalog,
        "supplementary_gap_context": packet.get("supplementary_context") or {},
        "selected_evidence": packet.get("selected_evidence") or [],
        "hard_rules": {
            "citation_ids_must_come_from_selected_evidence": True,
            "scientific_axis_catalog_is_open_world": True,
            "new_axis_requires_evidence_and_nonredundancy_reason": True,
            "do_not_infer_beyond_supplied_text": True,
        },
    }
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def sanitize_material_proposition_card(
    raw: Mapping[str, Any],
    packet: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fail closed on fabricated IDs, closed-world axes, and permission drift."""

    evidence_rows = [
        dict(row)
        for row in packet.get("selected_evidence") or []
        if isinstance(row, Mapping) and _text(row.get("chunk_id"), 240)
    ]
    evidence_by_id = {
        _text(row.get("chunk_id"), 240): row for row in evidence_rows
    }
    valid_chunk_ids = set(evidence_by_id)
    seed_axis_catalog = _seed_axis_catalog(packet)
    seed_axes = [
        dict(row)
        for row in seed_axis_catalog
        if isinstance(row, Mapping) and _text(row.get("axis_id"), 80)
    ]
    seed_by_id = {_text(row.get("axis_id"), 80): row for row in seed_axes}
    seed_labels = {
        _normalized_label(row.get("description")) for row in seed_axes
    }
    removed = {
        "unknown_chunk_ids": [],
        "unknown_seed_axis_ids": [],
        "invalid_seed_assignments": 0,
        "invalid_emergent_axes": 0,
        "redundant_emergent_axes": 0,
        "invalid_propositions": 0,
        "non_explicit_propositions": 0,
        "invalid_background_contexts": 0,
    }

    def valid_ids(value: Any, *, require: bool = True) -> list[str]:
        accepted = []
        for item in _values(value):
            if item in valid_chunk_ids:
                accepted.append(item)
            else:
                removed["unknown_chunk_ids"].append(item)
        accepted = list(dict.fromkeys(accepted))
        return accepted if accepted or not require else []

    relevance = _text(raw.get("question_relevance"), 40).casefold()
    if relevance not in _QUESTION_RELEVANCE:
        relevance = "contextual"
    functions = [
        value for value in _values(raw.get("paper_functions"))
        if value in _PAPER_FUNCTIONS
    ]

    assignments = []
    for item in raw.get("seed_axis_assignments") or []:
        if not isinstance(item, Mapping):
            removed["invalid_seed_assignments"] += 1
            continue
        axis_id = _text(item.get("axis_id"), 80)
        if axis_id not in seed_by_id:
            removed["unknown_seed_axis_ids"].append(axis_id)
            continue
        basis = valid_ids(item.get("basis_chunk_ids"))
        fit = _text(item.get("fit"), 40).casefold()
        question_function = _text(item.get("question_function"), 80).casefold()
        if not basis or fit not in {"central", "substantial", "contextual"}:
            removed["invalid_seed_assignments"] += 1
            continue
        if question_function not in _QUESTION_FUNCTIONS:
            question_function = "background_context"
        assignments.append(
            {
                "axis_id": axis_id,
                "axis_description": _text(seed_by_id[axis_id].get("description"), 500),
                "axis_origin": "user_question",
                "fit": fit,
                "question_function": question_function,
                "reason": _text(item.get("reason"), 700),
                "basis_chunk_ids": basis,
            }
        )

    emergent = []
    for item in raw.get("emergent_axis_candidates") or []:
        if not isinstance(item, Mapping):
            removed["invalid_emergent_axes"] += 1
            continue
        label = _text(item.get("label"), 160)
        definition = _text(item.get("definition"), 700)
        insufficiency = _text(
            item.get("why_seed_axes_are_insufficient"), 700
        )
        relationship = _text(item.get("relationship_to_question"), 700)
        level = _text(item.get("proposed_level"), 80).casefold()
        basis = valid_ids(item.get("basis_chunk_ids"))
        parent_ids = [
            value for value in _values(item.get("parent_seed_axis_ids"))
            if value in seed_by_id
        ]
        if _normalized_label(label) in seed_labels:
            removed["redundant_emergent_axes"] += 1
            continue
        if (
            not label
            or not definition
            or not insufficiency
            or not relationship
            or not basis
            or level not in _AXIS_LEVELS
        ):
            removed["invalid_emergent_axes"] += 1
            continue
        emergent.append(
            {
                "candidate_axis_id": "axis-candidate:"
                + hashlib.sha1(
                    (packet.get("canonical_work_id", "") + "|" + _normalized_label(label)).encode("utf-8")
                ).hexdigest()[:20],
                "label": label,
                "definition": definition,
                "origin": "material_emergent",
                "why_seed_axes_are_insufficient": insufficiency,
                "relationship_to_question": relationship,
                "proposed_level": level,
                "parent_seed_axis_ids": list(dict.fromkeys(parent_ids)),
                "basis_chunk_ids": basis,
                "promotion_status": "candidate_only",
            }
        )

    propositions = []
    for item in raw.get("propositions") or []:
        if not isinstance(item, Mapping):
            removed["invalid_propositions"] += 1
            continue
        statement = _text(item.get("statement"), 1600)
        kind = _text(item.get("proposition_kind"), 80).casefold()
        stance = _text(item.get("stance"), 80).casefold()
        question_function = _text(item.get("question_function"), 80).casefold()
        basis = valid_ids(item.get("evidence_chunk_ids"))
        if item.get("explicitly_stated") is not True:
            removed["non_explicit_propositions"] += 1
            continue
        if (
            len(statement) < 20
            or kind not in _PROPOSITION_KINDS
            or stance not in _PROPOSITION_STANCES
            or not basis
        ):
            removed["invalid_propositions"] += 1
            continue
        if question_function not in _QUESTION_FUNCTIONS:
            question_function = "background_context"
        ceilings = [
            _text(evidence_by_id[chunk_id].get("evidence_ceiling"), 80)
            for chunk_id in basis
        ]
        weakest = min(
            ceilings,
            key=lambda value: _CEILING_RANK.get(value, -1),
        )
        strongest = max(
            ceilings,
            key=lambda value: _CEILING_RANK.get(value, -1),
        )
        propositions.append(
            {
                "proposition_id": _stable_proposition_id(
                    str(packet.get("canonical_work_id") or ""), statement, basis
                ),
                "statement": statement,
                "proposition_kind": kind,
                "stance": stance,
                "question_function": question_function,
                "explicitly_stated": True,
                "evidence_chunk_ids": basis,
                "evidence_permissions": {
                    chunk_id: _text(
                        evidence_by_id[chunk_id].get("evidence_ceiling"), 80
                    )
                    for chunk_id in basis
                },
                "weakest_evidence_ceiling": weakest,
                "strongest_evidence_ceiling": strongest,
                "permission_was_not_model_assigned": True,
            }
        )

    backgrounds = []
    for item in raw.get("background_contexts") or []:
        if not isinstance(item, Mapping):
            removed["invalid_background_contexts"] += 1
            continue
        statement = _text(item.get("statement"), 1000)
        basis = valid_ids(item.get("basis_chunk_ids"))
        if len(statement) < 20 or not basis:
            removed["invalid_background_contexts"] += 1
            continue
        backgrounds.append(
            {
                "statement": statement,
                "basis_chunk_ids": basis,
                "use_class": "background_context",
            }
        )

    query_annotation = {
        "query_id": _text(packet.get("query_id"), 240),
        "question_hash": _text(packet.get("question_hash"), 120),
        "annotation_schema_version": _text(
            packet.get("annotation_schema_version"), 120
        ) or "optomind.query_annotation.v1",
        "model_version": _text(packet.get("model_version"), 160) or "unknown",
    }
    supplementary_reference = _supplementary_task_reference(packet)
    if supplementary_reference is not None:
        query_annotation["supplementary_task_reference"] = (
            supplementary_reference
        )
    card = {
        "schema_version": CARD_SCHEMA_VERSION,
        "query_annotation": query_annotation,
        "canonical_work_id": str(packet.get("canonical_work_id") or ""),
        "paper_identity": dict(packet.get("canonical_identity") or {}),
        "member_paper_ids": list(packet.get("member_paper_ids") or []),
        "material_classes": list(packet.get("material_classes") or []),
        "question_relevance": relevance,
        "paper_functions": list(dict.fromkeys(functions)),
        "question_seed_axes": seed_axes,
        "seed_axis_assignments": assignments,
        "emergent_axis_candidates": emergent,
        "propositions": propositions,
        "background_contexts": backgrounds,
        "extraction_warnings": _values(raw.get("extraction_warnings"))[:12],
        "source_packet_audit": dict(packet.get("selection_audit") or {}),
    }
    audit = {
        "status": "passed" if propositions or backgrounds else "empty",
        "valid_selected_chunk_count": len(valid_chunk_ids),
        "seed_assignment_count": len(assignments),
        "emergent_axis_candidate_count": len(emergent),
        "proposition_count": len(propositions),
        "background_context_count": len(backgrounds),
        "removed": {
            key: sorted(set(value)) if isinstance(value, list) else value
            for key, value in removed.items()
        },
        "identity_is_upstream_owned": True,
        "permissions_are_upstream_owned": True,
        "scientific_axes_are_open_world": True,
        "query_annotation_is_namespaced": True,
    }
    return card, audit


def extract_one_material_card(
    packet: Mapping[str, Any],
    *,
    model_tier: str = "b_plus_model",
    max_tokens: int = 4200,
    timeout_seconds: float = 180.0,
    max_retries: int = 1,
    format_retry_max_tokens: int = FORMAT_RETRY_MAX_TOKENS_DEFAULT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call the model, keep usage, and retry once on truncated JSON."""

    messages = build_material_extraction_messages(packet)

    def call_attempt(
        attempt_messages: list[dict[str, str]],
        *,
        attempt_max_tokens: int,
        attempt_max_retries: int,
    ) -> dict[str, Any]:
        return call_qwen_chat(
            "MaterialPropositionExtractor",
            attempt_messages,
            model_tier=model_tier,
            max_retries=attempt_max_retries,
            temperature=0.0,
            force_mock=False,
            max_tokens=attempt_max_tokens,
            response_format={"type": "json_object"},
            stream=True,
            timeout_seconds=timeout_seconds,
            max_transport_key_candidates=2,
            allow_model_fallback=False,
            accept_partial_stream=False,
            enable_thinking=False,
        )

    usage_rows: list[dict[str, Any]] = []
    response = call_attempt(
        messages,
        attempt_max_tokens=max_tokens,
        attempt_max_retries=max_retries,
    )
    usage_rows.append(dict(response.get("_llm_usage") or {}))
    try:
        raw = _parse_json_object(str(response.get("content") or ""))
        card, audit = sanitize_material_proposition_card(raw, packet)
    except Exception as first_error:
        retry_messages = [
            *messages,
            {"role": "user", "content": FORMAT_RETRY_INSTRUCTION},
        ]
        retry_response = call_attempt(
            retry_messages,
            attempt_max_tokens=max(8000, int(format_retry_max_tokens)),
            attempt_max_retries=0,
        )
        usage_rows.append(dict(retry_response.get("_llm_usage") or {}))
        try:
            raw = _parse_json_object(str(retry_response.get("content") or ""))
            card, audit = sanitize_material_proposition_card(raw, packet)
        except Exception as exc:
            merged_usage = _merge_usage_rows(usage_rows)
            raise MaterialCardExtractionError(
                "Material card JSON parse/validation failed after format "
                "retry: " + _text(exc, 600),
                usage=merged_usage,
                per_attempt_usage=usage_rows,
                format_retry_count=max(0, len(usage_rows) - 1),
            ) from exc
    merged_usage = _merge_usage_rows(usage_rows)
    audit["llm_usage"] = merged_usage
    audit["format_retry_count"] = int(
        merged_usage.get("format_retry_count") or 0
    )
    card["query_annotation"]["model_version"] = str(
        merged_usage.get("model_name") or model_tier
    )
    return card, audit


def _selected_sample(
    packets: list[dict[str, Any]],
    *,
    limit: int,
    balanced_sample: bool,
) -> list[dict[str, Any]]:
    if limit <= 0 or limit >= len(packets):
        return list(packets)
    if not balanced_sample:
        return list(packets[:limit])
    selected: list[dict[str, Any]] = []
    classes = ("s2_body", "oa_fulltext", "abstract_claim")
    for material_class in classes:
        row = next(
            (
                packet for packet in packets
                if material_class in packet.get("material_classes", [])
                and packet not in selected
            ),
            None,
        )
        if row is not None and len(selected) < limit:
            selected.append(row)
    for packet in packets:
        if len(selected) >= limit:
            break
        if packet not in selected:
            selected.append(packet)
    return selected


def run_material_proposition_extraction(
    *,
    packet_path: Path,
    output_dir: Path,
    model_tier: str = "b_plus_model",
    limit: int = 0,
    balanced_sample: bool = False,
    workers: int = 1,
    skip_existing: bool = True,
) -> dict[str, Any]:
    """Extract cards with bounded parallelism and per-work resumability."""

    payload = json.loads(packet_path.read_text(encoding="utf-8"))
    question = _text(payload.get("question"), 4000)
    question_hash = "sha256:" + hashlib.sha256(question.encode("utf-8")).hexdigest()
    query_id = "query:" + hashlib.sha256(question.encode("utf-8")).hexdigest()[:20]
    packets = [
        {
            **dict(item),
            "query_id": _text(item.get("query_id"), 240) or query_id,
            "question_hash": _text(item.get("question_hash"), 120) or question_hash,
            "annotation_schema_version": _text(
                item.get("annotation_schema_version"), 120
            ) or "optomind.query_annotation.v1",
        }
        for item in payload.get("packets") or []
        if isinstance(item, Mapping)
    ]
    selected = _selected_sample(
        packets, limit=max(0, int(limit)), balanced_sample=balanced_sample
    )
    cards_dir = output_dir / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)

    def card_path(packet: Mapping[str, Any]) -> Path:
        return cards_dir / f"{packet['canonical_work_id'].replace(':', '_')}.json"

    work: list[dict[str, Any]] = []
    reused = 0
    rows: list[dict[str, Any]] = []
    reused_rows: list[dict[str, Any]] = []
    for packet in selected:
        path = card_path(packet)
        if skip_existing and path.exists():
            # Backfill the question namespace on cards created by the earlier
            # v6 packet writer. This is metadata-only and never invokes Qwen.
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                card = value.get("card") if isinstance(value, dict) else None
                audit = value.get("audit") if isinstance(value, dict) else {}
                if isinstance(card, dict):
                    question = _text(packet.get("question"), 4000)
                    question_hash = _text(packet.get("question_hash"), 120) or (
                        "sha256:" + hashlib.sha256(question.encode("utf-8")).hexdigest()
                    )
                    query_id = _text(packet.get("query_id"), 240) or (
                        "query:" + hashlib.sha256(question.encode("utf-8")).hexdigest()[:20]
                    )
                    annotation = card.setdefault("query_annotation", {})
                    annotation["query_id"] = annotation.get("query_id") or query_id
                    annotation["question_hash"] = annotation.get("question_hash") or question_hash
                    annotation.setdefault("annotation_schema_version", "optomind.query_annotation.v1")
                    atomic_write_json(path, value)
                    usage = (
                        audit.get("llm_usage")
                        if isinstance(audit.get("llm_usage"), Mapping)
                        else {}
                    )
                    reused_rows.append({
                        "canonical_work_id": str(
                            card.get("canonical_work_id")
                            or packet.get("canonical_work_id")
                            or ""
                        ),
                        "status": "reused",
                        "card_path": str(path),
                        "proposition_count": len(
                            card.get("propositions") or []
                        ),
                        "emergent_axis_candidate_count": len(
                            card.get("emergent_axis_candidates") or []
                        ),
                        "llm_usage": dict(usage),
                        "per_attempt_usage": list(
                            usage.get("per_attempt_usage") or []
                        ),
                        "format_retry_count": int(
                            usage.get("format_retry_count") or 0
                        ),
                    })
            except Exception:
                # A corrupt existing card is left for the normal failed-card
                # report; do not hide it behind a metadata migration.
                pass
            reused += 1
            continue
        work.append(packet)

    def run_one(packet: dict[str, Any]) -> dict[str, Any]:
        work_id = str(packet.get("canonical_work_id") or "")
        try:
            card, audit = extract_one_material_card(
                packet, model_tier=model_tier
            )
            path = card_path(packet)
            atomic_write_json(
                path,
                {"card": card, "audit": audit},
            )
            return {
                "canonical_work_id": work_id,
                "status": audit.get("status"),
                "card_path": str(path),
                "proposition_count": len(card.get("propositions") or []),
                "emergent_axis_candidate_count": len(
                    card.get("emergent_axis_candidates") or []
                ),
                "llm_usage": audit.get("llm_usage") or {},
                "per_attempt_usage": (
                    (audit.get("llm_usage") or {}).get("per_attempt_usage")
                    or []
                ),
                "format_retry_count": audit.get("format_retry_count", 0),
            }
        except MaterialCardExtractionError as exc:
            return {
                "canonical_work_id": work_id,
                "status": "failed",
                "error_type": "MaterialCardExtractionError",
                "error": _text(exc, 1000),
                "llm_usage": exc.usage,
                "per_attempt_usage": exc.per_attempt_usage,
                "format_retry_count": exc.format_retry_count,
            }
        except Exception as exc:
            return {
                "canonical_work_id": work_id,
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": _text(exc, 1000),
            }

    if max(1, int(workers)) == 1:
        rows = [run_one(packet) for packet in work]
    else:
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
            future_map = {pool.submit(run_one, packet): packet for packet in work}
            for future in as_completed(future_map):
                rows.append(future.result())
    rows.extend(reused_rows)
    rows.sort(key=lambda item: str(item.get("canonical_work_id") or ""))

    for path in sorted(cards_dir.glob("work_*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            card = value.get("card") if isinstance(value, dict) else {}
            audit = value.get("audit") if isinstance(value, dict) else {}
            if isinstance(card, dict):
                # Existing/reused cards are needed by the aggregate JSONL even
                # when this invocation selected only a balanced smoke sample.
                pass
        except Exception:
            continue

    selected_ids = {
        str(packet.get("canonical_work_id") or "") for packet in selected
    }
    card_values = []
    for packet in selected:
        path = card_path(packet)
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        card = value.get("card") if isinstance(value, dict) else None
        if isinstance(card, dict) and card.get("canonical_work_id") in selected_ids:
            card_values.append(card)
    card_values.sort(key=lambda item: str(item.get("canonical_work_id") or ""))
    jsonl_path = output_dir / "MATERIAL_PROPOSITION_CARDS.jsonl"
    jsonl_path.write_text(
        "".join(json.dumps(card, ensure_ascii=False) + "\n" for card in card_values),
        encoding="utf-8",
    )

    all_rows = list(rows)
    summary = {
        "schema_version": RUN_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "packet_path": str(packet_path),
        "output_dir": str(output_dir),
        "model_tier": model_tier,
        "model_fallback_allowed": False,
        "selected_work_count": len(selected),
        "new_attempt_count": len(work),
        "reused_count": reused,
        "reused_usage_count": len(reused_rows),
        "successful_card_count": len(card_values),
        "failed_count": sum(1 for row in all_rows if row.get("status") == "failed"),
        "empty_count": sum(1 for row in all_rows if row.get("status") == "empty"),
        "format_retry_count": sum(
            int(row.get("format_retry_count") or 0)
            for row in all_rows
        ),
        "rows": all_rows,
        "cards_jsonl": str(jsonl_path),
        "supplemental_recall_triggered": False,
    }
    atomic_write_json(output_dir / "RUN_SUMMARY.json", summary)
    return summary


__all__ = [
    "CARD_SCHEMA_VERSION",
    "RUN_SCHEMA_VERSION",
    "build_material_extraction_messages",
    "sanitize_material_proposition_card",
    "extract_one_material_card",
    "run_material_proposition_extraction",
]
