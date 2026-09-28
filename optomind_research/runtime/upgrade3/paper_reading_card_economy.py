"""Lossless, lower-overhead model input for paper reading cards.

The original snapshot and the full card input remain the audit source.  This
module only constructs the model-facing view: repeated per-observation
identity/source fields are removed, section context is emitted once when it
changes, and a metadata abstract is omitted only when its normalized text is
an exact match for an article abstract.  No body text is truncated or fuzzy
deduplicated.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections import Counter
from typing import Any, Mapping, Sequence

from .paper_reading_card_prompt import build_messages


_SCHEMA_VERSION = "optomind.paper_reading_card.economy.v1"
_AUDIT_SCHEMA_VERSION = "optomind.paper_reading_card.economy_audit.v1"
_SPACE_RE = re.compile(r"\s+")


def _text(value: Any) -> str:
    """Return a text value without coercing structured values into prose."""

    return value if isinstance(value, str) else ""


def _normalized(value: str) -> str:
    return _SPACE_RE.sub(" ", value).strip()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _section_path(observation: Mapping[str, Any]) -> list[str]:
    path = observation.get("section_path")
    if path is None and isinstance(observation.get("source"), Mapping):
        path = observation["source"].get("section_path")
    if isinstance(path, str):
        return [path] if path else []
    if isinstance(path, Sequence) and not isinstance(path, (bytes, bytearray)):
        return [item for item in path if isinstance(item, str) and item]
    return []


def _source_kind(observation: Mapping[str, Any]) -> str:
    value = observation.get("source_kind")
    if not isinstance(value, str) and isinstance(observation.get("source"), Mapping):
        value = observation["source"].get("kind")
    return value if isinstance(value, str) and value else "unknown"


def _observations(packet: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = packet.get("observations")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _short_material(card_input: Mapping[str, Any]) -> dict[str, Any]:
    material = card_input.get("material")
    material = material if isinstance(material, Mapping) else {}
    result: dict[str, Any] = {}
    for key in ("material_scope", "declared_content_depth", "known_gaps"):
        if key in material:
            result[key] = copy.deepcopy(material[key])
    return result


def _short_identity(card_input: Mapping[str, Any]) -> dict[str, Any]:
    identity = card_input.get("paper_identity")
    identity = identity if isinstance(identity, Mapping) else {}
    return {
        "title": _text(identity.get("title")),
        "year": identity.get("year"),
    }


def _short_plan(card_input: Mapping[str, Any]) -> dict[str, Any]:
    plan = card_input.get("review_plan")
    plan = plan if isinstance(plan, Mapping) else {}
    result: dict[str, Any] = {}
    for key in (
        "schema_version",
        "question_en",
        "research_object",
        "criteria",
        "additional_constraints",
    ):
        if key in plan:
            result[key] = copy.deepcopy(plan[key])

    facets = plan.get("facets")
    if not isinstance(facets, list):
        facets = []
    result["facets"] = []
    for facet in facets:
        if not isinstance(facet, Mapping):
            continue
        compact_facet = {
            "id": facet.get("id", facet.get("facet_id")),
            "ask": _text(facet.get("ask", facet.get("question"))),
        }
        # Keep scope constraints, while dropping only query-expansion prose
        # that is useful to retrieval and irrelevant to reading the paper.
        for key in ("filters", "must_exclude"):
            if key in facet:
                compact_facet[key] = copy.deepcopy(facet[key])
        result["facets"].append(compact_facet)
    return result


def _marker(source_kind: str, section_path: Sequence[str]) -> str:
    path = " > ".join(section_path) if section_path else "(unsectioned)"
    return f"[section] {source_kind}: {path}\n"


def _compact_packet(
    packet: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    observations = _observations(packet)
    article_abstract_keys = {
        _normalized(_text(observation.get("text")))
        for observation in observations
        if _source_kind(observation) == "article_abstract"
        and _normalized(_text(observation.get("text")))
    }

    ordered_text: list[str] = []
    occurrence_map: list[dict[str, Any]] = []
    emitted_hashes: list[str] = []
    source_counts = Counter()
    emitted_source_counts = Counter()
    previous_group: tuple[str, tuple[str, ...]] | None = None
    deduped_count = 0
    empty_count = 0

    for observation_index, observation in enumerate(observations):
        raw_text = _text(observation.get("text"))
        normalized = _normalized(raw_text)
        source_kind = _source_kind(observation)
        section_path = _section_path(observation)
        group = (source_kind, tuple(section_path))
        source_counts[source_kind] += 1

        if not normalized:
            empty_count += 1
            occurrence_map.append(
                {
                    "observation_index": observation_index,
                    "status": "empty_observation",
                    "normalized_sha256": _digest(normalized),
                }
            )
            continue

        if source_kind == "metadata_abstract" and normalized in article_abstract_keys:
            kept_index = next(
                (
                    index
                    for index, candidate in enumerate(observations)
                    if _source_kind(candidate) == "article_abstract"
                    and _normalized(_text(candidate.get("text"))) == normalized
                ),
                None,
            )
            deduped_count += 1
            occurrence_map.append(
                {
                    "observation_index": observation_index,
                    "status": "deduplicated_exact_normalized",
                    "kept_observation_index": kept_index,
                    "normalized_sha256": _digest(normalized),
                }
            )
            continue

        output_index = len(ordered_text)
        prefix = _marker(source_kind, section_path) if group != previous_group else ""
        ordered_text.append(prefix + raw_text)
        emitted_hash = _digest(normalized)
        emitted_hashes.append(emitted_hash)
        emitted_source_counts[source_kind] += 1
        occurrence_map.append(
            {
                "observation_index": observation_index,
                "status": "emitted",
                "ordered_text_index": output_index,
                "normalized_sha256": emitted_hash,
            }
        )
        previous_group = group

    # The surrounding payload carries material depth and review identity.  The
    # packet therefore contains only the ordered scientific text; repeating
    # snapshot/material fields here would spend context without adding facts.
    compact_packet: dict[str, Any] = {"ordered_text": ordered_text}
    audit = {
        "schema_version": _AUDIT_SCHEMA_VERSION,
        "deduplication_policy": (
            "Only metadata_abstract is omitted when its whitespace-normalized "
            "text exactly matches an article_abstract; no fuzzy, substring, "
            "or body deduplication."
        ),
        "original_observation_count": len(observations),
        "emitted_text_occurrence_count": len(ordered_text),
        "deduplicated_occurrence_count": deduped_count,
        "empty_observation_count": empty_count,
        "source_kind_counts": dict(source_counts),
        "emitted_source_kind_counts": dict(emitted_source_counts),
        "emitted_normalized_text_sha256": emitted_hashes,
        "occurrence_map": occurrence_map,
        "removed_per_observation_fields": [
            "observation_id",
            "source",
            "paper_identity",
        ],
    }
    return compact_packet, audit


def build_economy_messages(
    card_input: Mapping[str, Any],
    concise_output: bool = False,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Build compact messages and an audit proving lossless text handling.

    ``card_input`` is never mutated.  The returned audit is intentionally
    occurrence-level so a caller can persist it beside the original input and
    independently verify every observed text was emitted or mapped to an
    exact normalized duplicate.
    """

    if not isinstance(card_input, Mapping):
        raise TypeError("card_input must be a mapping")
    packet = card_input.get("reading_packet")
    if not isinstance(packet, Mapping):
        raise ValueError("card_input.reading_packet is required")

    compact_packet, audit = _compact_packet(packet)
    prompt_input = {
        "prompt_version": card_input.get("prompt_version"),
        "paper_identity": _short_identity(card_input),
        "material": _short_material(card_input),
        "review_plan": _short_plan(card_input),
        "reading_packet": compact_packet,
    }
    messages = build_messages(prompt_input)
    if concise_output:
        if not messages or messages[0].get("role") != "system":
            raise ValueError("paper card prompt must begin with a system message")
        messages = [dict(message) for message in messages]
        messages[0]["content"] += (
            "\n\n本轮经济输入的篇幅策略覆盖上文关于 B 约 600–1200 token 的软目标，"
            "不改变 JSON schema 或事实边界："
            "A 通常约 250–450 token，B 通常约 350–650 token；"
            "内容丰富时允许超过；减少重复同一事实但保留条件和边界，"
            "不强迫少条目，材料不足时可为空。"
        )

    audit.update(
        {
            "card_id": card_input.get("card_id"),
            "snapshot_sha256": card_input.get("snapshot_sha256"),
            "topic_sha256": card_input.get("topic_sha256"),
            "prompt_version": card_input.get("prompt_version"),
            "concise_output": concise_output,
            "compact_packet_schema_version": _SCHEMA_VERSION,
        }
    )
    return messages, audit


__all__ = ["build_economy_messages"]
