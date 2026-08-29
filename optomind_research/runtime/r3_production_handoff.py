"""Versioned, deterministic R3 production handoff.

The Phase-3 pipeline produces several useful views of the same material.  A
writer must not have to decide which view is authoritative, or silently fill
in a missing one.  This module is the single typed boundary between Phase 3
and R4.

The implementation intentionally uses dataclasses and the standard library.
It does not discover literature, call an LLM, or infer scientific truth.  It
only normalizes the already-audited Phase-3 state and checks that every
reference is traceable, permission-compatible, and scoped to the active
topic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .argument_quality_policy import (
    BACKGROUND,
    DISCOVERY,
    FACTUAL,
    QUALIFIED,
    evidence_ceiling,
)


R3_HANDOFF_SCHEMA_VERSION = "research_harness.r3_production_handoff.v1"
R3_HANDOFF_VERSION = 1
R3_HANDOFF_FILENAME = "R3_PRODUCTION_HANDOFF.json"
R3_HANDOFF_KIND = "r3_production_handoff"
R3_CRITICALITIES = ("load_bearing", "supporting", "optional")
R3_CLAIM_CLASSIFICATIONS = ("supported", "qualified", "open_question")
R3_SECTION_OUTCOMES = (
    "ready",
    "ready_with_limits",
    "merge_required",
    "needs_more_literature",
)
R3_PERMISSION_STATUSES = frozenset({
    "bound",
    "qualified_only",
    "unbound",
    "unresolved",
    "needs_more_literature",
    "write_with_declared_gap",
    "write_with_qualified_support",
})
R3_WRITE_STATUSES = frozenset({
    "bound",
    "write_with_qualified_support",
    "write_with_declared_gap",
    "needs_more_literature",
    "unresolved",
})
R3_UNRESOLVED_STATES = frozenset({
    "open_question",
    "uncertain",
    "contested",
    "insufficient",
    "unresolved",
    "unverified",
    "needs_more_literature",
    "partially_grounded",
    "partial",
})
R3_ALLOWED_DAG_RELATIONS = frozenset({
    "depends_on",
    "supports",
    "motivates",
    "extends",
    "constrains",
    "limits",
    "applies_to",
    "qualifies",
    "contrasts_with",
})
R3_COMPATIBLE_SCHEMA_VERSIONS = frozenset({
    R3_HANDOFF_SCHEMA_VERSION,
    "r3.production_handoff.v1",
    "r3_production_handoff.v1",
})
R3_CANONICAL_SCHEMA_VERSIONS = frozenset({R3_HANDOFF_SCHEMA_VERSION})


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Mapping):
        return [value]
    return []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _unique(values: Iterable[Any] | None) -> list[str]:
    return list(dict.fromkeys(
        _text(value) for value in (values or ()) if _text(value)
    ))


def _lower(value: Any) -> str:
    return _text(value).casefold()


_IDENTITY_ALIAS_FIELDS = frozenset({
    "aliases",
    "aliasids",
    "paperaliases",
    "paperaliasids",
    "canonicalaliases",
    "identityaliases",
    "alternateids",
    "alternatepaperids",
    "externalaliases",
    "sourceids",
})
_IDENTITY_CONTAINER_FIELDS = frozenset({
    "externalids",
    "identifiers",
    "identity",
    "metadata",
    "rawmetadata",
    "provenance",
    "routeprovenance",
    "sourceidentity",
})
_DOI_FIELDS = frozenset({
    "doi",
    "doiid",
    "doiurl",
    "digitalobjectidentifier",
})
_CORPUS_FIELDS = frozenset({
    "corpusid",
    "s2id",
    "s2paperid",
    "semanticscholarid",
})
_LOCAL_ID_FIELDS = frozenset({
    "paperid",
    "localpaperid",
    "canonicalpaperid",
    "paperkey",
})
_PAPER_OWNER_FIELDS = (
    "paper_id",
    "parent_paper_id",
    "source_paper_id",
    "paperId",
    "parentPaperId",
    "sourcePaperId",
    "parent_id",
    "parentId",
)
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s<>\"']+", re.IGNORECASE)
_CORPUS_RE = re.compile(
    r"(?:corpus\s*id|corpusid|corpus_id|s2(?:\s*(?:paper\s*)?id)?|"
    r"semantic\s*scholar\s*id)\s*[:=_-]?\s*(\d+)",
    re.IGNORECASE,
)
_S2_HEX_RE = re.compile(r"(?:s2\s*[:=_-]\s*)?([0-9a-f]{40})\Z", re.IGNORECASE)
_OBSERVED_DISCOVERY_EDGE_TYPES = frozenset({
    "cites",
    "cited_by",
    "semantic_recommendation",
    "s2_recommended",
    "snippet_ref_mention",
    "co_cited_with",
    "bibliographic_coupling",
    "same_research_branch",
    "supports_same_claim",
})


def _normalised_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _text(value).casefold())


def _normalise_doi(value: Any) -> str:
    """Return one stable DOI spelling without treating it as a local ID."""

    text = _text(value).casefold()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)
    text = re.sub(r"^(?:doi\s*:\s*|doi/)", "", text)
    match = _DOI_RE.search(text)
    if match:
        text = match.group(0)
    return text.strip(" \t\r\n.,;:)}]>\"")


def _normalise_corpus_id(value: Any) -> str:
    text = _text(value).casefold()
    match = _CORPUS_RE.fullmatch(text)
    if match:
        return match.group(1)
    if text.isdigit():
        return text
    return ""


def _identity_alias_keys(value: Any, *, hint: str = "") -> list[str]:
    """Build typed lookup keys for a declared identity value.

    The untyped local key is intentionally added only as a literal identity
    key.  DOI, CorpusId, and S2 keys are added only when the value has that
    shape (or the containing field explicitly declares the type).
    """

    raw = _text(value)
    if not raw:
        return []
    hint_key = _normalised_key(hint)
    keys: list[str] = []

    def add(key: str) -> None:
        if key and key not in keys:
            keys.append(key)

    doi = _normalise_doi(raw)
    looks_like_doi = bool(
        "/" in doi and re.match(r"^10\.\d{4,9}/", doi, re.IGNORECASE)
    )
    if looks_like_doi or hint_key in _DOI_FIELDS:
        if looks_like_doi:
            add(f"doi:{doi}")

    corpus_id = _normalise_corpus_id(raw)
    if corpus_id and (
        raw.isdigit()
        or hint_key in _CORPUS_FIELDS
        or bool(_CORPUS_RE.fullmatch(raw.casefold()))
    ):
        add(f"s2:corpus:{corpus_id}")

    s2_match = _S2_HEX_RE.fullmatch(raw.strip())
    if s2_match and (
        hint_key in _CORPUS_FIELDS
        or raw.strip().casefold() == s2_match.group(1).casefold()
    ):
        add(f"s2:{s2_match.group(1).casefold()}")

    # Every declared paper_id/local alias also receives a literal lookup key.
    # This is what keeps unknown arbitrary identifiers from becoming papers:
    # a value resolves only when this exact typed key was derived from an
    # active record or an explicitly declared alias.
    add(f"local:{raw.casefold()}")
    return keys


def _chunk_id_identity_candidates(chunk_id: Any) -> list[tuple[str, str]]:
    """Extract only the DOI/S2 forms used by known canonical chunk IDs."""

    text = _text(chunk_id)
    if not text:
        return []
    candidates: list[tuple[str, str]] = []
    parts = text.split(":")
    prefix = parts[0].casefold() if parts else ""
    if prefix in {"m3gap", "m3_gap", "doi_chunk"} and len(parts) >= 2:
        segment = parts[1]
        if re.match(r"^10\.\d{4,9}/", segment, re.IGNORECASE):
            candidates.append((segment, "doi"))
    if prefix in {"s2chunk", "s2_chunk", "s2"} and len(parts) >= 2:
        segment = parts[1]
        if segment.isdigit():
            candidates.append((f"CorpusId:{segment}", "corpus_id"))
        elif _S2_HEX_RE.fullmatch(segment):
            candidates.append((segment, "s2_id"))
    for index, segment in enumerate(parts):
        segment_key = segment.casefold()
        if segment_key in {"doi", "doi_id", "doichunk"} and index + 1 < len(parts):
            segment = parts[index + 1]
        if re.match(r"^10\.\d{4,9}/", segment, re.IGNORECASE):
            candidates.append((segment.split(":", 1)[0], "doi"))
    for match in _DOI_RE.finditer(text):
        candidates.append((match.group(0).rstrip(".,;:)}]>"), "doi"))
    for match in _CORPUS_RE.finditer(text):
        candidates.append((f"CorpusId:{match.group(1)}", "corpus_id"))
    return list(dict.fromkeys(candidates))


def _record_identity_candidates(
    raw: Any,
    *,
    record_kind: str,
) -> list[tuple[str, str, bool]]:
    """Read only identity-shaped fields from paper/chunk records."""

    row = _record_mapping(raw)
    candidates: list[tuple[str, str, bool]] = []

    def add(value: Any, hint: str = "", explicit: bool = False) -> None:
        if _text(value):
            candidates.append((_text(value), hint, explicit))

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 3 or not isinstance(value, Mapping):
            return
        for key, item in value.items():
            normalized = _normalised_key(key)
            if normalized in _IDENTITY_ALIAS_FIELDS:
                if isinstance(item, Mapping):
                    for alias_key, alias_value in item.items():
                        add(alias_key, "", True)
                        add(alias_value, "", True)
                else:
                    for alias in _identifier_values(item):
                        add(alias, "", True)
                continue
            if normalized in _DOI_FIELDS:
                for item_value in _identifier_values(item):
                    add(item_value, "doi", True)
                continue
            if normalized in _CORPUS_FIELDS:
                for item_value in _identifier_values(item):
                    add(item_value, normalized, True)
                continue
            if normalized in _LOCAL_ID_FIELDS:
                for item_value in _identifier_values(item):
                    add(item_value, "paper_id", True)
                continue
            if record_kind == "papers" and normalized == "id":
                for item_value in _identifier_values(item):
                    add(item_value, "paper_id", True)
                continue
            if normalized in _IDENTITY_CONTAINER_FIELDS:
                if isinstance(item, Mapping):
                    walk(item, depth + 1)

    walk(row)
    if record_kind == "chunks":
        for value, hint in _chunk_id_identity_candidates(row.get("chunk_id")):
            add(value, hint, True)
    return candidates


def _record_mapping(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if hasattr(raw, "to_dict"):
        try:
            value = raw.to_dict()
            return dict(value) if isinstance(value, Mapping) else {}
        except Exception:
            pass
    if hasattr(raw, "__dataclass_fields__"):
        return {
            name: getattr(raw, name)
            for name in raw.__dataclass_fields__
            if hasattr(raw, name)
        }
    return {}


def _identifier_values(value: Any) -> list[Any]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, Mapping):
        return []
    return [value] if value not in (None, "") else []


def _identity_records_map(value: Any, id_key: str) -> dict[str, dict[str, Any]]:
    """Normalize mapping/dataclass record containers for resolver input."""

    if isinstance(value, Mapping):
        if isinstance(value.get(id_key), str):
            row = _record_mapping(value)
            identifier = _text(row.get(id_key))
            return {identifier: row} if identifier else {}
        if isinstance(value.get("items"), (Mapping, list, tuple)):
            return _identity_records_map(value.get("items"), id_key)
        result: dict[str, dict[str, Any]] = {}
        for key, raw in value.items():
            row = _record_mapping(raw)
            if not row:
                continue
            row.setdefault(id_key, _text(key))
            identifier = _text(row.get(id_key))
            if identifier:
                result[identifier] = row
        return result
    result = {}
    for raw in _list(value):
        row = _record_mapping(raw)
        if not row:
            continue
        identifier = _text(row.get(id_key))
        if identifier:
            result[identifier] = row
    return result


@dataclass
class CanonicalIdentityResolver:
    """Deterministic alias resolver for the active paper inventory."""

    active_paper_ids: tuple[str, ...] = ()
    alias_map: dict[str, str] = field(default_factory=dict)
    ambiguous_aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    _alias_candidates: dict[str, set[str]] = field(default_factory=dict, repr=False)

    @classmethod
    def from_inventory(cls, material_inventory: Mapping[str, Any] | None) -> "CanonicalIdentityResolver":
        inventory = _dict(material_inventory)
        paper_rows = _identity_records_map(inventory.get("papers"), "paper_id")
        chunk_rows = _identity_records_map(inventory.get("chunks"), "chunk_id")
        active_ids = tuple(sorted(_text(identifier) for identifier in paper_rows if _text(identifier)))
        resolver = cls(active_paper_ids=active_ids)

        for identifier, raw in paper_rows.items():
            canonical = _text(identifier)
            if not canonical:
                continue
            resolver._add_value(canonical, canonical, hint="paper_id")
            for value, hint, _explicit in _record_identity_candidates(raw, record_kind="papers"):
                resolver._add_value(value, canonical, hint=hint)

        # Chunk ownership is a second identity source.  It is accepted only
        # when the chunk's owner or its structured DOI/S2 chunk form resolves
        # to exactly one active paper.
        for _chunk_id, raw in chunk_rows.items():
            row = _record_mapping(raw)
            owner_values = []
            for field_name in _PAPER_OWNER_FIELDS:
                if field_name in row:
                    owner_values.extend(_identifier_values(row.get(field_name)))
            owners = {
                resolved
                for value in owner_values
                for resolved in [resolver.resolve(value, hint="paper_id")]
                if resolved
            }
            chunk_identity_values = _record_identity_candidates(row, record_kind="chunks")
            chunk_aliases = [
                (value, hint)
                for value, hint, _explicit in chunk_identity_values
            ]
            if len(owners) != 1:
                inferred_owners = {
                    resolved
                    for value, hint in chunk_aliases
                    for resolved in [resolver.resolve(value, hint=hint)]
                    if resolved
                }
                if len(inferred_owners) == 1:
                    owners = inferred_owners
            if len(owners) == 1:
                owner = next(iter(owners))
                for value, hint in chunk_aliases:
                    existing = resolver.resolve(value, hint=hint)
                    if existing and existing != owner:
                        continue
                    resolver._add_value(value, owner, hint=hint)

        resolver._finalise()
        return resolver

    def _add_value(self, value: Any, canonical: str, *, hint: str = "") -> None:
        if canonical not in self.active_paper_ids:
            return
        for key in _identity_alias_keys(value, hint=hint):
            self._alias_candidates.setdefault(key, set()).add(canonical)

    def _finalise(self) -> None:
        self.alias_map = {}
        self.ambiguous_aliases = {}
        for key, candidates in sorted(self._alias_candidates.items()):
            if len(candidates) == 1:
                self.alias_map[key] = next(iter(candidates))
            elif candidates:
                self.ambiguous_aliases[key] = tuple(sorted(candidates))

    def resolve(self, value: Any, *, hint: str = "") -> str:
        keys = _identity_alias_keys(value, hint=hint)
        local_keys = [key for key in keys if key.startswith("local:")]
        local_candidates: set[str] = set()
        for key in local_keys:
            if key in self.ambiguous_aliases:
                return ""
            local_candidates.update(self._alias_candidates.get(key, set()))
        if len(local_candidates) == 1:
            return next(iter(local_candidates))
        if len(local_candidates) > 1:
            return ""

        candidates: set[str] = set()
        for key in keys:
            if key.startswith("local:"):
                continue
            mapped = self._alias_candidates.get(key, set())
            candidates.update(mapped)
            if key in self.ambiguous_aliases:
                return ""
        return next(iter(candidates)) if len(candidates) == 1 else ""

    def resolve_visual_parent(self, raw: Any) -> tuple[str, str]:
        row = _record_mapping(raw)
        values: list[Any] = []
        for field_name in _PAPER_OWNER_FIELDS:
            if field_name in row:
                values.extend(_identifier_values(row.get(field_name)))
        resolved = {
            value
            for raw_value in values
            for value in [self.resolve(raw_value, hint="paper_id")]
            if value
        }
        if len(resolved) == 1:
            return next(iter(resolved)), "mapped_alias"
        if len(resolved) > 1:
            return "", "ambiguous_parent_identity"
        return "", "unmapped_parent_identity"

    def map_relation_endpoints(
        self,
        edges: Iterable[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Map known relation endpoints while retaining unresolved provenance."""

        output: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for raw in edges:
            if not isinstance(raw, Mapping):
                continue
            edge = dict(raw)
            source_raw = _text(edge.get("source_paper_id") or edge.get("source_id"))
            target_raw = _text(edge.get("target_paper_id") or edge.get("target_id"))
            source = self.resolve(source_raw, hint="paper_id")
            target = self.resolve(target_raw, hint="paper_id")
            if source:
                edge["source_paper_id"] = source
                if "source_id" in edge:
                    edge["source_id"] = source
            if target:
                edge["target_paper_id"] = target
                if "target_id" in edge:
                    edge["target_id"] = target
            reason = (
                "mapped_both_endpoints"
                if source and target
                else "unmapped_source_endpoint"
                if not source
                else "unmapped_target_endpoint"
            )
            counts[reason] = counts.get(reason, 0) + 1
            output.append(edge)
        return output, {
            "input_edges": len(output),
            "mapped_edges": counts.get("mapped_both_endpoints", 0),
            "unmapped_edges": len(output) - counts.get("mapped_both_endpoints", 0),
            "reasons": dict(sorted(counts.items())),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "research_harness.canonical_identity_resolver.v1",
            "active_paper_ids": list(self.active_paper_ids),
            "alias_map": dict(sorted(self.alias_map.items())),
            "ambiguous_aliases": {
                key: list(value)
                for key, value in sorted(self.ambiguous_aliases.items())
            },
        }


def build_canonical_identity_resolver(
    material_inventory: Mapping[str, Any] | None,
) -> CanonicalIdentityResolver:
    """Build the active-paper resolver without discovery or network access."""

    return CanonicalIdentityResolver.from_inventory(material_inventory)


def _normalise_criticality(raw: Mapping[str, Any] | None) -> str:
    row = _dict(raw)
    value = _lower(
        row.get("criticality")
        or row.get("importance")
        or row.get("importance_level")
        or row.get("priority")
    )
    if value not in R3_CRITICALITIES:
        value = "load_bearing" if bool(row.get("load_bearing")) else "supporting"
    return value


def _normalise_claim_classification(
    raw: Mapping[str, Any] | None,
    binding: Mapping[str, Any] | None = None,
) -> str:
    """Read the closed claim classification without upgrading evidence."""

    row = _dict(raw)
    binding_row = _dict(binding)
    value = _lower(
        row.get("support_classification")
        or row.get("claim_classification")
        or binding_row.get("support_classification")
        or binding_row.get("claim_classification")
    )
    if value in R3_CLAIM_CLASSIFICATIONS:
        return value
    state = _lower(
        row.get("claim_state")
        or row.get("status")
        or binding_row.get("claim_state")
    )
    if state in {"open_question", "unresolved", "unsupported", "unverified", "needs_more_literature"}:
        return "open_question"
    if state in {"partial", "partially_grounded", "qualified", "conditional"}:
        return "qualified"
    permission = _lower(binding_row.get("permission_status") or row.get("permission_status"))
    if permission in {"qualified_only", "contextual_or_qualified_support"}:
        return "qualified"
    if permission == "bound":
        return "supported"
    if _unique(
        _list(row.get("supporting_text_chunk_ids"))
        + _list(row.get("supporting_chunk_ids"))
        + _list(row.get("factual_support_chunk_ids"))
        + _list(row.get("contextual_support_chunk_ids"))
    ):
        return "supported"
    return "open_question"


def _claim_rows(value: Any) -> list[dict[str, Any]]:
    """Normalize list/map claim containers without inventing asset IDs."""

    if isinstance(value, Mapping):
        if isinstance(value.get("claims"), (Mapping, list, tuple)):
            return _claim_rows(value.get("claims"))
        rows: list[dict[str, Any]] = []
        for key, raw in value.items():
            if isinstance(raw, Mapping):
                row = dict(raw)
                row.setdefault("claim_id", _text(key))
                rows.append(row)
        return rows
    return [dict(item) for item in _list(value) if isinstance(item, Mapping)]


def _section_map(value: Any, *, item_key: str = "section_id") -> dict[str, dict[str, Any]]:
    if isinstance(value, Mapping):
        if isinstance(value.get("sections"), (Mapping, list, tuple)):
            return _section_map(value.get("sections"), item_key=item_key)
        result: dict[str, dict[str, Any]] = {}
        for key, raw in value.items():
            if isinstance(raw, Mapping):
                row = dict(raw)
                row.setdefault(item_key, _text(key))
                sid = _text(row.get(item_key))
                if sid:
                    result[sid] = row
        return result
    result = {}
    for raw in _list(value):
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        sid = _text(row.get(item_key))
        if sid:
            result[sid] = row
    return result


def _records_map(value: Any, id_key: str) -> dict[str, dict[str, Any]]:
    if isinstance(value, Mapping):
        if isinstance(value.get(id_key), str):
            row = dict(value)
            return {_text(row[id_key]): row}
        if isinstance(value.get("items"), (Mapping, list, tuple)):
            return _records_map(value.get("items"), id_key)
        result: dict[str, dict[str, Any]] = {}
        for key, raw in value.items():
            if isinstance(raw, Mapping):
                row = dict(raw)
                row.setdefault(id_key, _text(key))
                identifier = _text(row.get(id_key))
                if identifier:
                    result[identifier] = row
        return result
    result = {}
    for raw in _list(value):
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        identifier = _text(row.get(id_key))
        if identifier:
            result[identifier] = row
    return result


def _normalise_contracts(value: Any) -> dict[str, dict[str, Any]]:
    raw = value.get("contracts") if isinstance(value, Mapping) and "contracts" in value else value
    return _section_map(raw)


def _normalise_bundles(value: Any) -> dict[str, dict[str, Any]]:
    raw = value.get("bundles") if isinstance(value, Mapping) and "bundles" in value else value
    return _section_map(raw)


def _normalise_bindings(value: Any) -> dict[str, dict[str, Any]]:
    raw = value.get("sections") if isinstance(value, Mapping) and "sections" in value else value
    return _section_map(raw)


def _normalise_visual_map(value: Any, sections: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    result = {str(section_id): [] for section_id in sections if _text(section_id)}
    if isinstance(value, Mapping):
        if isinstance(value.get("bindings"), (Mapping, list, tuple)):
            value = value.get("bindings")
        elif isinstance(value.get("needs"), (Mapping, list, tuple)):
            value = value.get("needs")
        if isinstance(value, Mapping):
            for section_id, raw in value.items():
                if _text(section_id):
                    result[_text(section_id)] = [
                        dict(item) for item in _list(raw) if isinstance(item, Mapping)
                    ]
            return result
    for raw in _list(value):
        if not isinstance(raw, Mapping):
            continue
        section_id = _text(raw.get("section_id"))
        if section_id:
            result.setdefault(section_id, []).append(dict(raw))
    return result


def _as_serializable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _as_serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_as_serializable(item) for item in value]
    if hasattr(value, "to_dict"):
        try:
            return _as_serializable(value.to_dict())
        except Exception:
            pass
    if hasattr(value, "__dataclass_fields__"):
        try:
            return _as_serializable({
                name: getattr(value, name)
                for name in value.__dataclass_fields__
            })
        except Exception:
            pass
    return value


@dataclass
class CoverageAtlas:
    """Typed wrapper around the deterministic section coverage atlas."""

    schema_version: str = "research_harness.coverage_atlas.v1"
    sections: list[dict[str, Any]] = field(default_factory=list)
    relation_graph: dict[str, Any] = field(default_factory=dict)
    topic_identity: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any) -> "CoverageAtlas":
        row = _dict(raw)
        known = {"schema_version", "sections", "relation_graph", "topic_identity", "source"}
        return cls(
            schema_version=_text(row.get("schema_version") or "research_harness.coverage_atlas.v1"),
            sections=[dict(item) for item in _list(row.get("sections")) if isinstance(item, Mapping)],
            relation_graph=_dict(row.get("relation_graph")),
            topic_identity=_dict(row.get("topic_identity")),
            source=_dict(row.get("source")),
            extra={key: value for key, value in row.items() if key not in known},
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "sections": sorted(
                [dict(item) for item in self.sections],
                key=lambda item: _text(item.get("section_id")),
            ),
            "relation_graph": dict(self.relation_graph),
            "topic_identity": dict(self.topic_identity),
            "source": dict(self.source),
        }
        payload.update(self.extra)
        return payload


@dataclass
class R3SectionArgumentContract:
    """Typed section contract; unknown legacy fields are retained verbatim."""

    section_id: str
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any, section_id: str = "") -> "R3SectionArgumentContract":
        payload = _dict(raw)
        sid = _text(payload.get("section_id") or section_id)
        payload["section_id"] = sid
        return cls(section_id=sid, payload=payload)

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.payload)
        payload["section_id"] = self.section_id
        return payload


@dataclass
class R3Claim:
    claim_id: str
    section_id: str
    statement: str
    criticality: str
    claim_state: str = ""
    evidence_type: str = ""
    unresolved: bool = False
    unresolved_reasons: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any, *, criticality: str = "") -> "R3Claim":
        payload = _dict(raw)
        claim_id = _text(payload.get("claim_id"))
        section_id = _text(payload.get("section_id"))
        normalized = criticality if criticality in R3_CRITICALITIES else _normalise_criticality(payload)
        payload["criticality"] = normalized
        if _lower(
            payload.get("support_classification")
            or payload.get("claim_classification")
        ):
            payload["support_classification"] = _normalise_claim_classification(payload)
        reasons = _unique(
            _list(payload.get("unresolved_reasons"))
            + _list(payload.get("missing_evidence_components"))
            + _list(payload.get("missing_components"))
        )
        return cls(
            claim_id=claim_id,
            section_id=section_id,
            statement=_text(
                payload.get("statement")
                or payload.get("original_statement")
                or payload.get("claim")
            ),
            criticality=normalized,
            claim_state=_lower(payload.get("claim_state") or payload.get("status")),
            evidence_type=_lower(payload.get("evidence_type")),
            unresolved=bool(payload.get("unresolved"))
            or _lower(payload.get("claim_state") or payload.get("status")) in R3_UNRESOLVED_STATES
            or bool(reasons),
            unresolved_reasons=reasons,
            payload=payload,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.payload)
        payload.update({
            "claim_id": self.claim_id,
            "section_id": self.section_id,
            "statement": self.statement,
            "criticality": self.criticality,
            "claim_state": self.claim_state,
            "evidence_type": self.evidence_type,
            "unresolved": self.unresolved,
            "unresolved_reasons": list(self.unresolved_reasons),
        })
        return payload


@dataclass
class R3MaterialBinding:
    section_id: str
    claims: dict[str, dict[str, Any]] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any, section_id: str = "") -> "R3MaterialBinding":
        payload = _dict(raw)
        sid = _text(payload.get("section_id") or section_id)
        payload["section_id"] = sid
        claim_container = payload.get("claims")
        if isinstance(claim_container, Mapping):
            claims = {
                _text(key): dict(value)
                for key, value in _dict(claim_container).items()
                if _text(key) and isinstance(value, Mapping)
            }
        else:
            claims = {
                _text(item.get("claim_id")): dict(item)
                for item in _claim_rows(claim_container)
                if _text(item.get("claim_id"))
            }
        return cls(section_id=sid, claims=claims, payload=payload)

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.payload)
        payload["section_id"] = self.section_id
        payload["claims"] = {key: dict(value) for key, value in self.claims.items()}
        return payload


@dataclass
class R3CompactSynthesisBundle:
    section_id: str
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any, section_id: str = "") -> "R3CompactSynthesisBundle":
        payload = _dict(raw)
        sid = _text(payload.get("section_id") or section_id)
        payload["section_id"] = sid
        return cls(section_id=sid, payload=payload)

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.payload)
        payload["section_id"] = self.section_id
        return payload


@dataclass
class R3ValidationReport:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    section_readiness: dict[str, dict[str, Any]] = field(default_factory=dict)
    global_readiness: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return "passed" if self.valid else "failed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "sections": {key: dict(value) for key, value in self.section_readiness.items()},
            "global": dict(self.global_readiness),
        }


class R3HandoffValidationError(ValueError):
    """Raised by ``require_valid`` when the canonical handoff is unsafe."""

    def __init__(self, report: R3ValidationReport):
        self.report = report
        message = "R3 production handoff is invalid: " + "; ".join(report.errors[:8])
        super().__init__(message)


@dataclass
class R3ProductionHandoff:
    """The complete, versioned, authoring-bound R3 handoff."""

    schema_version: str = R3_HANDOFF_SCHEMA_VERSION
    handoff_version: int = R3_HANDOFF_VERSION
    handoff_kind: str = R3_HANDOFF_KIND
    topic_identity: dict[str, Any] = field(default_factory=dict)
    sections: list[dict[str, Any]] = field(default_factory=list)
    coverage_atlas: CoverageAtlas = field(default_factory=CoverageAtlas)
    section_argument_contracts: dict[str, R3SectionArgumentContract] = field(default_factory=dict)
    claims_by_criticality: dict[str, list[R3Claim]] = field(
        default_factory=lambda: {key: [] for key in R3_CRITICALITIES}
    )
    material_inventory: dict[str, dict[str, dict[str, Any]]] = field(
        default_factory=lambda: {"papers": {}, "chunks": {}, "visuals": {}}
    )
    material_bindings: dict[str, R3MaterialBinding] = field(default_factory=dict)
    relation_graph: dict[str, Any] = field(default_factory=lambda: {"edges": []})
    claim_dag: dict[str, Any] = field(default_factory=lambda: {"edges": []})
    gaps: list[dict[str, Any]] = field(default_factory=list)
    coverage_requests: list[dict[str, Any]] = field(default_factory=list)
    synthesis_bundles: dict[str, R3CompactSynthesisBundle] = field(default_factory=dict)
    visual_bindings: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    visual_needs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    readiness: dict[str, Any] = field(default_factory=dict)
    source_artifacts: dict[str, Any] = field(default_factory=dict)
    legacy_migration: dict[str, Any] = field(default_factory=dict)
    identity_resolution: dict[str, Any] = field(default_factory=dict)

    @property
    def section_ids(self) -> list[str]:
        return [
            _text(item.get("section_id"))
            for item in self.sections
            if isinstance(item, Mapping) and _text(item.get("section_id"))
        ]

    @property
    def claims(self) -> list[R3Claim]:
        return [
            claim
            for criticality in R3_CRITICALITIES
            for claim in self.claims_by_criticality.get(criticality, [])
        ]

    @classmethod
    def from_dict(cls, raw: Any) -> "R3ProductionHandoff":
        row = _dict(raw)
        section_rows = [
            dict(item) if isinstance(item, Mapping) else {"section_id": _text(item)}
            for item in _list(row.get("sections"))
            if isinstance(item, Mapping) or _text(item)
        ] if "sections" in row else []
        section_ids = _unique(item.get("section_id") for item in section_rows)

        contract_rows = _normalise_contracts(row.get("section_argument_contracts"))
        contracts = {
            sid: R3SectionArgumentContract.from_dict(contract_rows.get(sid, {}), sid)
            for sid in contract_rows
        }
        grouped: dict[str, list[R3Claim]] = {key: [] for key in R3_CRITICALITIES}
        grouped_raw = row.get("claims_by_criticality")
        if isinstance(grouped_raw, Mapping):
            for criticality, values in grouped_raw.items():
                normalized = _lower(criticality)
                for claim in _claim_rows(values):
                    claim_obj = R3Claim.from_dict(claim, criticality=normalized)
                    grouped.setdefault(normalized, []).append(claim_obj)
        if not any(grouped.values()):
            for claim in _claim_rows(row.get("claims")):
                claim_obj = R3Claim.from_dict(claim)
                grouped.setdefault(claim_obj.criticality, []).append(claim_obj)
        for criticality in R3_CRITICALITIES:
            grouped.setdefault(criticality, [])

        inventory_raw = (
            row.get("material_inventory")
            or row.get("evidence_catalog")
            or row.get("material_catalog")
            or {}
        )
        inventory = {} if not inventory_raw else {
            "papers": _records_map(_dict(inventory_raw).get("papers"), "paper_id"),
            "chunks": _records_map(_dict(inventory_raw).get("chunks"), "chunk_id"),
            "visuals": _records_map(
                _dict(inventory_raw).get("visuals")
                or _dict(inventory_raw).get("visual_chunks"),
                "visual_id",
            ),
        }
        binding_rows = _normalise_bindings(row.get("material_bindings"))
        bindings = {
            sid: R3MaterialBinding.from_dict(value, sid)
            for sid, value in binding_rows.items()
        }
        for claim in [item for rows in grouped.values() for item in rows]:
            if not _lower(
                claim.payload.get("support_classification")
                or claim.payload.get("claim_classification")
            ):
                binding = bindings.get(claim.section_id)
                raw_binding = binding.claims.get(claim.claim_id, {}) if binding else {}
                classification = _normalise_claim_classification(
                    claim.payload,
                    raw_binding,
                )
                claim.payload["support_classification"] = classification
                claim.payload.setdefault("claim_classification", classification)
        bundle_rows = _normalise_bundles(row.get("synthesis_bundles"))
        bundles = {
            sid: R3CompactSynthesisBundle.from_dict(value, sid)
            for sid, value in bundle_rows.items()
        }
        coverage_requests = row.get("coverage_requests")
        if isinstance(coverage_requests, Mapping):
            coverage_requests = coverage_requests.get("requests") or []
        gaps = row.get("gaps") or row.get("unresolved_gaps") or []
        return cls(
            schema_version=_text(row.get("schema_version") or ""),
            handoff_version=int(row.get("handoff_version") or 0),
            handoff_kind=_text(row.get("handoff_kind") or ""),
            topic_identity=_dict(row.get("topic_identity")),
            sections=section_rows,
            coverage_atlas=CoverageAtlas.from_dict(row.get("coverage_atlas")),
            section_argument_contracts=contracts,
            claims_by_criticality=grouped,
            material_inventory=inventory,
            material_bindings=bindings,
            relation_graph=(
                _dict(row.get("relation_graph"))
                if "relation_graph" in row else {"_missing": True}
            ),
            claim_dag=(
                _dict(row.get("claim_dag") or row.get("claim_graph"))
                if "claim_dag" in row or "claim_graph" in row else {"_missing": True}
            ),
            gaps=[dict(item) for item in _list(gaps) if isinstance(item, Mapping)],
            coverage_requests=[
                dict(item) for item in _list(coverage_requests) if isinstance(item, Mapping)
            ],
            synthesis_bundles=bundles,
            visual_bindings=(
                _normalise_visual_map(row.get("visual_bindings"), section_ids)
                if "visual_bindings" in row else {}
            ),
            visual_needs=(
                _normalise_visual_map(row.get("visual_needs"), section_ids)
                if "visual_needs" in row else {}
            ),
            readiness=_dict(row.get("readiness")),
            source_artifacts=_dict(row.get("source_artifacts")),
            legacy_migration=_dict(row.get("legacy_migration")),
            identity_resolution=_dict(row.get("identity_resolution")),
        )

    def to_dict(self) -> dict[str, Any]:
        def stable_rows(value: Any, *keys: str) -> list[dict[str, Any]]:
            rows = [dict(item) for item in _list(value) if isinstance(item, Mapping)]
            return sorted(
                rows,
                key=lambda item: tuple(_text(item.get(key)) for key in keys)
                or (_text(item.get("id")),),
            )

        grouped = {
            criticality: [
                claim.to_dict()
                for claim in sorted(
                    self.claims_by_criticality.get(criticality, []),
                    key=lambda item: (item.claim_id, item.section_id),
                )
            ]
            for criticality in R3_CRITICALITIES
        }
        payload = {
            "schema_version": self.schema_version,
            "handoff_version": self.handoff_version,
            "handoff_kind": self.handoff_kind,
            "topic_identity": dict(self.topic_identity),
            "sections": stable_rows(self.sections, "section_id"),
            "coverage_atlas": self.coverage_atlas.to_dict(),
            "section_argument_contracts": {
                sid: contract.to_dict()
                for sid, contract in sorted(self.section_argument_contracts.items())
            },
            "claims_by_criticality": grouped,
            "claims": [claim for rows in grouped.values() for claim in rows],
            "material_inventory": {
                kind: {
                    identifier: dict(record)
                    for identifier, record in sorted(self.material_inventory.get(kind, {}).items())
                }
                for kind in ("papers", "chunks", "visuals")
            },
            "material_bindings": {
                sid: binding.to_dict()
                for sid, binding in sorted(self.material_bindings.items())
            },
            "relation_graph": {
                **dict(self.relation_graph),
                "edges": stable_rows(
                    self.relation_graph.get("edges") or self.relation_graph.get("relations") or [],
                    "edge_id", "source_paper_id", "target_paper_id",
                ),
            },
            "claim_dag": {
                **dict(self.claim_dag),
                "edges": stable_rows(
                    self.claim_dag.get("edges") or self.claim_dag.get("relations") or [],
                    "edge_id", "source_claim_id", "target_claim_id",
                ),
            },
            "gaps": stable_rows(self.gaps, "gap_id", "section_id"),
            "unresolved_gaps": stable_rows(self.gaps, "gap_id", "section_id"),
            "coverage_requests": stable_rows(self.coverage_requests, "request_id", "section_id"),
            "synthesis_bundles": {
                sid: bundle.to_dict()
                for sid, bundle in sorted(self.synthesis_bundles.items())
            },
            "visual_bindings": {
                sid: stable_rows(rows, "visual_binding_id", "visual_id")
                for sid, rows in sorted(self.visual_bindings.items())
            },
            "visual_needs": {
                sid: stable_rows(rows, "need_id", "visual_need_id")
                for sid, rows in sorted(self.visual_needs.items())
            },
            "readiness": dict(self.readiness),
            "source_artifacts": dict(self.source_artifacts),
            "legacy_migration": dict(self.legacy_migration),
            "identity_resolution": dict(self.identity_resolution),
        }
        return _as_serializable(payload)

    def validate(self) -> R3ValidationReport:
        report = validate_r3_production_handoff(self)
        self.readiness = {
            "sections": report.section_readiness,
            "global": report.global_readiness,
            "validation": report.to_dict(),
        }
        return report

    def require_valid(self) -> R3ValidationReport:
        report = self.validate()
        if not report.valid:
            raise R3HandoffValidationError(report)
        return report


def _topic_markers(value: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in ("topic_id", "topic_key", "topic_slug", "topic_fingerprint", "review_topic_id"):
        if _text(value.get(key)):
            result[key] = _text(value.get(key))
    return result


def _topic_mismatches(value: Any, expected: Mapping[str, str], path: str = "") -> list[str]:
    mismatches: list[str] = []
    if isinstance(value, Mapping):
        for key, raw in value.items():
            key_text = _text(key)
            current_path = f"{path}.{key_text}" if path else key_text
            if key_text in {"topic_identity", "topic"} and isinstance(raw, Mapping):
                for marker, actual in _topic_markers(raw).items():
                    expected_value = expected.get(marker)
                    if expected_value and actual != expected_value:
                        mismatches.append(
                            f"stale_topic_artifact:{current_path}.{marker}:{actual}!={expected_value}"
                        )
                continue
            if key_text in {"topic_id", "topic_key", "topic_slug", "topic_fingerprint", "review_topic_id"}:
                actual = _text(raw)
                expected_value = expected.get(key_text)
                if expected_value and actual and actual != expected_value:
                    mismatches.append(
                        f"stale_topic_artifact:{current_path}:{actual}!={expected_value}"
                    )
            if isinstance(raw, (Mapping, list, tuple)):
                mismatches.extend(_topic_mismatches(raw, expected, current_path))
    elif isinstance(value, (list, tuple)):
        for index, raw in enumerate(value):
            mismatches.extend(_topic_mismatches(raw, expected, f"{path}[{index}]"))
    return mismatches


def _inventory_record(
    inventory: Mapping[str, Mapping[str, dict[str, Any]]],
    kind: str,
    identifier: str,
    section_id: str = "",
) -> dict[str, Any] | None:
    row = inventory.get(kind, {}).get(identifier)
    if not isinstance(row, Mapping):
        return None
    result = dict(row)
    overrides = row.get("permissions_by_section")
    if isinstance(overrides, Mapping) and isinstance(overrides.get(section_id), Mapping):
        result.update(dict(overrides.get(section_id)))
    return result


def _binding_ids(binding: Mapping[str, Any]) -> dict[str, list[str]]:
    def collect(names: Iterable[str]) -> list[str]:
        values: list[str] = []
        for name in names:
            values.extend(_list(binding.get(name)))
        return _unique(values)

    return {
        "supporting_chunks": collect((
            "supporting_chunk_ids",
            "supporting_text_chunk_ids",
            "support_chunk_ids",
            "factual_support_chunk_ids",
            "contextual_support_chunk_ids",
            "context_support_chunk_ids",
        )),
        "factual_chunks": collect(("factual_support_chunk_ids", "direct_support_chunk_ids")),
        "contextual_chunks": collect(("contextual_support_chunk_ids", "context_support_chunk_ids")),
        "core_chunks": collect(("core_chunk_ids", "core_text_chunk_ids")),
        "candidate_chunks": collect(("candidate_chunk_ids",)),
        "papers": collect(("paper_ids", "supporting_paper_ids", "core_paper_ids", "citation_paper_ids")),
        "visuals": collect(("visual_chunk_ids", "visual_asset_ids", "supporting_visual_chunk_ids")),
    }


def _edge_rows(graph: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = graph.get("edges") or graph.get("relations") or []
    return [dict(item) for item in _list(raw) if isinstance(item, Mapping)]


def _basis_ids(edge: Mapping[str, Any]) -> list[str]:
    values: list[Any] = []
    for field_name in (
        "relation_basis_chunk_ids",
        "basis_chunk_ids",
        "relation_basis_chunk_id",
        "basis_chunk_id",
        "source_chunk_ids",
        "target_chunk_ids",
        "source_chunk_id",
        "target_chunk_id",
    ):
        values.extend(_identifier_values(edge.get(field_name)))
    return _unique(values)


def _explicit_unresolved(claim: R3Claim, binding: Mapping[str, Any] | None = None) -> bool:
    row = _dict(binding)
    state = _lower(
        claim.claim_state
        or claim.payload.get("claim_state")
        or claim.payload.get("status")
    )
    return bool(
        claim.unresolved
        or row.get("unresolved")
        or state in R3_UNRESOLVED_STATES
        or _lower(row.get("write_status")) in {"needs_more_literature", "unresolved"}
        or bool(_list(row.get("missing_evidence_components") or row.get("missing_components")))
    )


def _has_unresolved_reason(claim: R3Claim, binding: Mapping[str, Any] | None = None) -> bool:
    row = _dict(binding)
    return bool(
        claim.unresolved_reasons
        or _list(row.get("unresolved_reasons"))
        or _list(row.get("missing_evidence_components") or row.get("missing_components"))
        or _lower(claim.claim_state) in R3_UNRESOLVED_STATES
        or _lower(row.get("write_status")) in {"needs_more_literature", "unresolved"}
    )


def _topological_cycle(edges: Iterable[Mapping[str, Any]]) -> bool:
    rows = [dict(edge) for edge in edges]
    adjacency: dict[str, set[str]] = {}
    indegree: dict[str, int] = {}
    for edge in rows:
        source = _text(edge.get("source_claim_id") or edge.get("source_id"))
        target = _text(edge.get("target_claim_id") or edge.get("target_id"))
        if not source or not target or not bool(edge.get("is_dag_backbone", True)):
            continue
        adjacency.setdefault(source, set()).add(target)
        adjacency.setdefault(target, set())
        indegree.setdefault(source, 0)
        indegree[target] = indegree.get(target, 0) + 1
    queue = sorted(node for node, degree in indegree.items() if degree == 0)
    visited = 0
    while queue:
        node = queue.pop(0)
        visited += 1
        for child in sorted(adjacency.get(node, set())):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
                queue.sort()
    return visited != len(indegree)


def _compact_samples(values: Iterable[Any], limit: int = 5) -> list[str]:
    return sorted(_unique(values))[:limit]


def _canonicalise_paper_id_fields(
    row: dict[str, Any],
    fields: Iterable[str],
    resolver: CanonicalIdentityResolver,
) -> None:
    for field_name in fields:
        if field_name not in row:
            continue
        values = _identifier_values(row.get(field_name))
        canonical_values = [
            resolver.resolve(value, hint="paper_id") or _text(value)
            for value in values
            if _text(value)
        ]
        row[field_name] = _unique(canonical_values)


def _filter_excluded_visual_ids(
    row: dict[str, Any],
    fields: Iterable[str],
    excluded_ids: set[str],
) -> list[str]:
    removed: list[str] = []
    for field_name in fields:
        if field_name not in row:
            continue
        values = _identifier_values(row.get(field_name))
        kept: list[str] = []
        for value in values:
            identifier = _text(value)
            if not identifier:
                continue
            if identifier in excluded_ids:
                removed.append(identifier)
            else:
                kept.append(identifier)
        row[field_name] = _unique(kept)
    return _unique(removed)


def _basis_is_authoring_eligible(
    edge: Mapping[str, Any],
    inventory: Mapping[str, Mapping[str, dict[str, Any]]],
) -> tuple[bool, str]:
    basis = _basis_ids(edge)
    if not basis:
        return False, "missing_basis_chunks"
    section_id = _text(edge.get("section_id"))
    for chunk_id in basis:
        record = _inventory_record(inventory, "chunks", chunk_id, section_id)
        if record is None:
            return False, "unknown_basis_chunks"
        if evidence_ceiling(record)[0] in {DISCOVERY, BACKGROUND}:
            return False, "basis_not_evidence_eligible"
    return True, ""


def _mark_discovery_relation_edge(
    edge: Mapping[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    row = dict(edge)
    semantic = _text(row.get("semantic_relation"))
    if semantic:
        row["observed_semantic_relation"] = semantic
        row["semantic_relation"] = ""
    old_status = _text(row.get("status"))
    if old_status and old_status != "discovery_lead":
        row["original_status"] = old_status
    row["status"] = "discovery_lead" if "endpoint" in reason else "unverified"
    row["authoring_eligible"] = False
    row["identity_resolution_reason"] = reason
    return row


def _is_observed_discovery_edge(edge: Mapping[str, Any]) -> bool:
    observed = _lower(edge.get("observed_relation") or edge.get("edge_type"))
    origin = _lower(edge.get("edge_origin") or edge.get("source"))
    return (
        observed in _OBSERVED_DISCOVERY_EDGE_TYPES
        or origin in {"s2_api", "semantic_scholar", "s2", "s2_graph"}
    )


def _ensure_canonical_identity_resolution(handoff: R3ProductionHandoff) -> dict[str, Any]:
    """Apply the identity boundary exactly once to a handoff in memory."""

    if handoff.identity_resolution.get("applied") is True:
        return handoff.identity_resolution

    inventory = handoff.material_inventory
    chunk_inventory = (
        inventory.get("chunks")
        if isinstance(inventory.get("chunks"), Mapping)
        else {}
    )
    visual_inventory = (
        inventory.get("visuals")
        if isinstance(inventory.get("visuals"), Mapping)
        else {}
    )
    resolver = build_canonical_identity_resolver(inventory)
    audit: dict[str, Any] = {
        "schema_version": "research_harness.identity_resolution.v1",
        "applied": True,
        "active_paper_ids": list(resolver.active_paper_ids),
        "alias_map": dict(sorted(resolver.alias_map.items())),
        "ambiguous_aliases": {
            key: list(value)
            for key, value in sorted(resolver.ambiguous_aliases.items())
        },
        "resolver": resolver.to_dict(),
    }

    # Normalize chunk ownership before any basis/evidence checks.  Unknown
    # owners are deliberately left intact so the strict validator can still
    # report a truly invented chunk owner.
    chunk_owner_unresolved: list[str] = []
    for chunk_id, raw in list(chunk_inventory.items()):
        row = dict(_record_mapping(raw))
        owner_raw = _text(row.get("paper_id"))
        owner = resolver.resolve(owner_raw, hint="paper_id") if owner_raw else ""
        if not owner:
            inferred_owners = {
                resolved
                for value, hint, _explicit in _record_identity_candidates(
                    row,
                    record_kind="chunks",
                )
                for resolved in [resolver.resolve(value, hint=hint)]
                if resolved
            }
            if len(inferred_owners) == 1:
                owner = next(iter(inferred_owners))
        if owner:
            if owner_raw != owner:
                if owner_raw:
                    row["original_paper_id"] = owner_raw
            row["paper_id"] = owner
        elif owner_raw:
            chunk_owner_unresolved.append(f"{chunk_id}:{owner_raw}")
        chunk_inventory[chunk_id] = row

    # Visuals with an active DOI/CorpusId/local alias are safe to retain.  A
    # visual with no active parent is a discovery lead, not authoring evidence;
    # remove it from the authoring inventory and retain only a compact audit.
    original_visuals = dict(visual_inventory)
    retained_visuals: dict[str, dict[str, Any]] = {}
    excluded_visual_ids: set[str] = set()
    excluded_visual_rows: list[dict[str, Any]] = []
    for visual_id, raw in original_visuals.items():
        row = dict(_record_mapping(raw))
        canonical, reason = resolver.resolve_visual_parent(row)
        if canonical:
            original_owner = _text(row.get("paper_id"))
            if original_owner and original_owner != canonical:
                row["original_paper_id"] = original_owner
            row["paper_id"] = canonical
            for field_name in _PAPER_OWNER_FIELDS:
                if field_name in row and _text(row.get(field_name)):
                    mapped = resolver.resolve(row.get(field_name), hint="paper_id")
                    if mapped:
                        row[field_name] = mapped
            retained_visuals[visual_id] = row
        else:
            excluded_visual_ids.add(_text(visual_id))
            excluded_visual_rows.append({
                "visual_id": _text(visual_id),
                "parent_identity": _text(
                    row.get("paper_id")
                    or row.get("parent_paper_id")
                    or row.get("source_paper_id")
                ),
                "status": "discovery_lead",
                "reason": reason,
            })
    if "visuals" in inventory and isinstance(inventory.get("visuals"), Mapping):
        inventory["visuals"] = retained_visuals
    audit["visuals"] = {
        "input_count": len(original_visuals),
        "authoring_count": len(retained_visuals),
        "excluded_discovery_lead_count": len(excluded_visual_rows),
        "excluded_discovery_lead_samples": excluded_visual_rows[:5],
    }

    # Paper references in authoring payloads use the same canonical local ID.
    paper_reference_fields = (
        "paper_ids",
        "core_paper_ids",
        "supporting_paper_ids",
        "citation_paper_ids",
        "core_papers",
    )
    for claim in handoff.claims:
        _canonicalise_paper_id_fields(claim.payload, paper_reference_fields, resolver)
        _filter_excluded_visual_ids(
            claim.payload,
            ("supporting_visual_chunk_ids", "visual_chunk_ids", "visual_asset_ids"),
            excluded_visual_ids,
        )
    for binding in handoff.material_bindings.values():
        for row in binding.claims.values():
            _canonicalise_paper_id_fields(row, paper_reference_fields, resolver)
            _filter_excluded_visual_ids(
                row,
                ("visual_chunk_ids", "visual_asset_ids", "supporting_visual_chunk_ids"),
                excluded_visual_ids,
            )
    for bundle in handoff.synthesis_bundles.values():
        _canonicalise_paper_id_fields(bundle.payload, paper_reference_fields, resolver)
        _filter_excluded_visual_ids(
            bundle.payload,
            ("visual_chunk_ids", "visual_asset_ids"),
            excluded_visual_ids,
        )

    for section_id, rows in list(handoff.visual_bindings.items()):
        kept_rows: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            visual_id = _text(
                row.get("visual_id")
                or row.get("visual_chunk_id")
                or row.get("visual_asset_id")
            )
            if visual_id in excluded_visual_ids:
                continue
            kept_rows.append(row)
        handoff.visual_bindings[section_id] = kept_rows
    for rows in handoff.visual_needs.values():
        for row in rows:
            visual_id = _text(row.get("visual_id"))
            if visual_id in excluded_visual_ids:
                row["discovery_lead"] = True
                row["satisfied"] = False

    # Relation graph edges with mapped endpoints and evidence-eligible basis
    # chunks are authoring relations.  Every other observed edge remains in a
    # separate audited discovery graph and cannot be consumed by R4.
    raw_graph = dict(handoff.relation_graph)
    has_relation_container = "edges" in raw_graph or "relations" in raw_graph
    mapped_edges, map_audit = resolver.map_relation_endpoints(
        _edge_rows(raw_graph)
    )
    authoring_edges: list[dict[str, Any]] = []
    discovery_edges: list[dict[str, Any]] = []
    relation_reasons: dict[str, int] = {}
    strict_endpoint_errors: list[str] = []
    for edge in mapped_edges:
        source = resolver.resolve(
            edge.get("source_paper_id") or edge.get("source_id"),
            hint="paper_id",
        )
        target = resolver.resolve(
            edge.get("target_paper_id") or edge.get("target_id"),
            hint="paper_id",
        )
        if not source or not target:
            reason = (
                "unmapped_source_endpoint"
                if not source
                else "unmapped_target_endpoint"
            )
            if not source and not target:
                reason = "unmapped_both_endpoints"
            if not _is_observed_discovery_edge(edge):
                edge_id = _text(edge.get("edge_id")) or "unknown"
                if not source:
                    strict_endpoint_errors.append(
                        f"invented_paper_id:relation_source:{edge_id}:"
                        f"{_text(edge.get('source_paper_id') or edge.get('source_id'))}"
                    )
                if not target:
                    strict_endpoint_errors.append(
                        f"invented_paper_id:relation_target:{edge_id}:"
                        f"{_text(edge.get('target_paper_id') or edge.get('target_id'))}"
                    )
        else:
            eligible, basis_reason = _basis_is_authoring_eligible(edge, inventory)
            reason = basis_reason if not eligible else ""
        if source and target and not reason:
            edge["source_paper_id"] = source
            edge["target_paper_id"] = target
            basis = _basis_ids(edge)
            if basis:
                # This is a normalization of an already supplied basis field,
                # never a basis inferred from a claim or a paper pair.
                edge["relation_basis_chunk_ids"] = basis
            edge["authoring_eligible"] = True
            authoring_edges.append(edge)
            continue
        relation_reasons[reason] = relation_reasons.get(reason, 0) + 1
        discovery_edges.append(
            _mark_discovery_relation_edge(edge, reason=reason or "unverified_relation")
        )
    relation_graph = dict(raw_graph)
    if has_relation_container:
        relation_graph["edges"] = authoring_edges
    if discovery_edges:
        relation_graph["discovery_edges"] = discovery_edges
    relation_graph["identity_resolution"] = {
        "authoring_edge_count": len(authoring_edges),
        "discovery_edge_count": len(discovery_edges),
        "reasons": dict(sorted(relation_reasons.items())),
    }
    handoff.relation_graph = relation_graph
    audit["relations"] = {
        "input_count": len(mapped_edges),
        "authoring_count": len(authoring_edges),
        "discovery_count": len(discovery_edges),
        "endpoint_mapping": map_audit,
        "reasons": dict(sorted(relation_reasons.items())),
        "strict_endpoint_error_count": len(set(strict_endpoint_errors)),
        "strict_endpoint_errors": _compact_samples(strict_endpoint_errors),
    }

    # A claim DAG edge without a supplied, valid basis is also not an
    # authoring relation.  Keep valid-endpoint exclusions in an audit view;
    # leave malformed claim endpoints in place so strict invented-claim
    # detection remains active.
    raw_dag = dict(handoff.claim_dag)
    has_dag_container = "edges" in raw_dag or "relations" in raw_dag
    dag_authoring: list[dict[str, Any]] = []
    dag_excluded: list[dict[str, Any]] = []
    claim_ids = {claim.claim_id for claim in handoff.claims if claim.claim_id}
    dag_reasons: dict[str, int] = {}
    for edge in _edge_rows(raw_dag):
        source = _text(edge.get("source_claim_id") or edge.get("source_id"))
        target = _text(edge.get("target_claim_id") or edge.get("target_id"))
        eligible, reason = _basis_is_authoring_eligible(edge, inventory)
        if not eligible and source in claim_ids and target in claim_ids:
            dag_reasons[reason] = dag_reasons.get(reason, 0) + 1
            dag_excluded.append(
                _mark_discovery_relation_edge(edge, reason=f"claim_dag_{reason}")
            )
        else:
            dag_authoring.append(dict(edge))
    dag = dict(raw_dag)
    if has_dag_container:
        dag["edges"] = dag_authoring
    if dag_excluded:
        dag["excluded_edges"] = dag_excluded
    handoff.claim_dag = dag
    audit["claim_dag"] = {
        "input_count": len(dag_authoring) + len(dag_excluded),
        "authoring_count": len(dag_authoring),
        "excluded_count": len(dag_excluded),
        "reasons": dict(sorted(dag_reasons.items())),
    }

    # Keep the atlas internally honest: its observed/discovery summary may
    # still be useful for audit, but its authoring edge count is the filtered
    # graph that R4 is permitted to consume.
    atlas_relation = dict(handoff.coverage_atlas.relation_graph)
    if atlas_relation:
        observed_count = atlas_relation.get("edge_count")
        if observed_count not in (None, ""):
            atlas_relation.setdefault("observed_edge_count", observed_count)
        if "edges" in atlas_relation or "relations" in atlas_relation:
            atlas_relation["edges"] = authoring_edges
            if discovery_edges:
                atlas_relation["discovery_edges"] = discovery_edges
        atlas_relation["authoring_edge_count"] = len(authoring_edges)
        atlas_relation["discovery_edge_count"] = len(discovery_edges)
        if "edge_count" in atlas_relation:
            atlas_relation["edge_count"] = len(authoring_edges)
        handoff.coverage_atlas.relation_graph = atlas_relation

    audit["unresolved_chunk_owner_count"] = len(chunk_owner_unresolved)
    audit["unresolved_chunk_owner_samples"] = _compact_samples(chunk_owner_unresolved)
    handoff.identity_resolution = audit
    return audit


def _validation_error_group(message: str) -> str:
    parts = _text(message).split(":")
    if not parts:
        return _text(message)
    if parts[0] in {
        "invented_paper_id",
        "invented_chunk_id",
        "invented_visual_id",
        "invented_claim_id",
        "invented_section_id",
    } and len(parts) >= 2:
        return ":".join(parts[:2])
    if parts[0] in {
        "relation_requires_basis_chunks",
        "relation_basis_not_evidence_eligible",
        "claim_dag_relation_requires_basis_chunks",
        "claim_dag_basis_not_evidence_eligible",
        "discovery_only_cannot_support_claim",
    }:
        return parts[0]
    return _text(message)


def _compact_validation_messages(messages: Iterable[str]) -> list[str]:
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for message in messages:
        value = _text(message)
        if not value:
            continue
        group = _validation_error_group(value)
        if group not in grouped:
            grouped[group] = []
            order.append(group)
        if value not in grouped[group]:
            grouped[group].append(value)
    result: list[str] = []
    for group in order:
        values = grouped[group]
        if len(values) == 1:
            result.append(values[0])
            continue
        samples = " | ".join(sorted(values)[:3])
        result.append(f"{group}:count={len(values)}:samples={samples}")
    return result


def validate_r3_production_handoff(
    handoff: R3ProductionHandoff | Mapping[str, Any],
) -> R3ValidationReport:
    """Strictly validate a handoff and deterministically derive readiness.

    A valid report may still be ``needs_more_literature``: explicit
    unresolved gaps are an honest production result.  ``valid=False`` means
    the handoff itself is unsafe or internally inconsistent and R4 must stop.
    """

    h = handoff if isinstance(handoff, R3ProductionHandoff) else R3ProductionHandoff.from_dict(handoff)
    identity_audit = _ensure_canonical_identity_resolution(h)
    errors: list[str] = []
    warnings: list[str] = []
    section_errors: dict[str, list[str]] = {}
    section_warnings: dict[str, list[str]] = {}

    def add_error(message: str, section_id: str = "") -> None:
        message = _text(message)
        if message not in errors:
            errors.append(message)
        if section_id:
            section_errors.setdefault(section_id, []).append(message)

    def add_warning(message: str, section_id: str = "") -> None:
        message = _text(message)
        if message not in warnings:
            warnings.append(message)
        if section_id:
            section_warnings.setdefault(section_id, [])
            if message not in section_warnings[section_id]:
                section_warnings[section_id].append(message)

    visual_audit = _dict(identity_audit.get("visuals"))
    relation_audit = _dict(identity_audit.get("relations"))
    dag_audit = _dict(identity_audit.get("claim_dag"))
    if int(visual_audit.get("excluded_discovery_lead_count") or 0):
        add_warning(
            "identity_resolution:visuals_excluded_as_discovery_leads:"
            + str(visual_audit.get("excluded_discovery_lead_count"))
        )
    if int(relation_audit.get("discovery_count") or 0):
        add_warning(
            "identity_resolution:relations_downgraded_to_discovery:"
            + str(relation_audit.get("discovery_count"))
        )
    if int(dag_audit.get("excluded_count") or 0):
        add_warning(
            "identity_resolution:claim_dag_edges_excluded_without_basis:"
            + str(dag_audit.get("excluded_count"))
        )
    strict_endpoint_errors = _list(relation_audit.get("strict_endpoint_errors"))
    for strict_error in strict_endpoint_errors:
        add_error(_text(strict_error))
    strict_endpoint_error_count = int(
        relation_audit.get("strict_endpoint_error_count") or len(strict_endpoint_errors)
    )
    if strict_endpoint_error_count > len(strict_endpoint_errors):
        add_error(
            "invented_paper_id:relation_endpoint:count="
            + str(strict_endpoint_error_count)
        )
    if identity_audit.get("ambiguous_aliases"):
        add_warning(
            "identity_resolution:ambiguous_aliases:"
            + str(len(identity_audit.get("ambiguous_aliases") or {}))
        )

    # A legacy spelling is not accepted by the canonical validator.  Callers
    # must invoke the explicit migration helper first so the provenance of the
    # compatibility step remains visible in ``legacy_migration``.
    if h.schema_version not in R3_CANONICAL_SCHEMA_VERSIONS:
        add_error(f"incompatible_schema_version:{h.schema_version or 'missing'}")
    if h.handoff_version != R3_HANDOFF_VERSION:
        add_error(f"incompatible_handoff_version:{h.handoff_version}")
    if h.handoff_kind != R3_HANDOFF_KIND:
        add_error(f"invalid_handoff_kind:{h.handoff_kind or 'missing'}")
    if "_missing" in h.relation_graph or not (
        isinstance(h.relation_graph, Mapping)
        and ("edges" in h.relation_graph or "relations" in h.relation_graph)
    ):
        add_error("missing_relation_graph")
    if "_missing" in h.claim_dag or not (
        isinstance(h.claim_dag, Mapping)
        and ("edges" in h.claim_dag or "relations" in h.claim_dag)
    ):
        add_error("missing_claim_dag")

    section_ids = h.section_ids
    if not section_ids:
        add_error("missing_sections")
    if len(section_ids) != len(set(section_ids)):
        add_error("duplicate_section_ids")
    known_sections = set(section_ids)

    expected_topic = _topic_markers(h.topic_identity)
    if expected_topic:
        # The handoff's own top-level identity is authoritative.  Every
        # nested artifact marker must agree with it; mismatches are stale
        # artifacts, not a recoverable missing field.
        nested_payload = h.to_dict()
        nested_payload.pop("topic_identity", None)
        for mismatch in _topic_mismatches(nested_payload, expected_topic):
            add_error(mismatch)

    contract_ids = set(h.section_argument_contracts)
    missing = sorted(known_sections - contract_ids)
    extra = sorted(contract_ids - known_sections)
    if missing:
        add_error("missing_section_argument_contracts:" + ",".join(missing))
    if extra:
        add_error("stale_section_argument_contracts:" + ",".join(extra))

    atlas_ids = {
        _text(row.get("section_id"))
        for row in h.coverage_atlas.sections
        if isinstance(row, Mapping) and _text(row.get("section_id"))
    }
    if atlas_ids != known_sections:
        missing = sorted(known_sections - atlas_ids)
        extra = sorted(atlas_ids - known_sections)
        if missing:
            add_error("missing_coverage_atlas_sections:" + ",".join(missing))
        if extra:
            add_error("stale_coverage_atlas_sections:" + ",".join(extra))

    binding_ids = set(h.material_bindings)
    if binding_ids != known_sections:
        missing = sorted(known_sections - binding_ids)
        extra = sorted(binding_ids - known_sections)
        if missing:
            add_error("missing_material_bindings:" + ",".join(missing))
        if extra:
            add_error("stale_material_bindings:" + ",".join(extra))

    bundle_ids = set(h.synthesis_bundles)
    if bundle_ids != known_sections:
        missing = sorted(known_sections - bundle_ids)
        extra = sorted(bundle_ids - known_sections)
        if missing:
            add_error("missing_synthesis_bundles:" + ",".join(missing))
        if extra:
            add_error("stale_synthesis_bundles:" + ",".join(extra))

    if set(h.visual_bindings) != known_sections:
        add_error("visual_bindings_do_not_cover_all_sections")
    if set(h.visual_needs) != known_sections:
        add_error("visual_needs_do_not_cover_all_sections")

    inventory = h.material_inventory
    for kind in ("papers", "chunks", "visuals"):
        if not isinstance(inventory.get(kind), Mapping):
            add_error(f"missing_material_inventory:{kind}")
    paper_ids = set(inventory.get("papers", {}))
    chunk_ids = set(inventory.get("chunks", {}))
    visual_ids = set(inventory.get("visuals", {}))

    for identifier, row in inventory.get("papers", {}).items():
        if _text(row.get("paper_id") or identifier) != identifier:
            add_error(f"material_inventory_id_mismatch:paper:{identifier}")
    for identifier, row in inventory.get("chunks", {}).items():
        if _text(row.get("chunk_id") or identifier) != identifier:
            add_error(f"material_inventory_id_mismatch:chunk:{identifier}")
        owner = _text(row.get("paper_id"))
        if owner and owner not in paper_ids:
            add_error(f"invented_paper_id:chunk:{identifier}:{owner}")
    for identifier, row in inventory.get("visuals", {}).items():
        if _text(row.get("visual_id") or identifier) != identifier:
            add_error(f"material_inventory_id_mismatch:visual:{identifier}")
        owner = _text(row.get("paper_id"))
        if owner and owner not in paper_ids:
            add_error(f"invented_paper_id:visual:{identifier}:{owner}")

    claims_by_id: dict[str, R3Claim] = {}
    claims_by_section: dict[str, list[R3Claim]] = {sid: [] for sid in known_sections}
    for criticality, claims in h.claims_by_criticality.items():
        if criticality not in R3_CRITICALITIES:
            add_error(f"invalid_claim_criticality:{criticality}")
            continue
        for claim in claims:
            if claim.criticality != criticality:
                add_error(f"claim_criticality_mismatch:{claim.claim_id}:{criticality}", claim.section_id)
            if not claim.claim_id:
                add_error("claim_missing_id", claim.section_id)
                continue
            if claim.claim_id in claims_by_id:
                add_error(f"duplicate_claim_id:{claim.claim_id}", claim.section_id)
                continue
            if claim.section_id not in known_sections:
                add_error(f"invented_section_id:claim:{claim.claim_id}:{claim.section_id}")
            if not claim.statement:
                add_error(f"claim_missing_statement:{claim.claim_id}", claim.section_id)
            raw_classification = _lower(
                claim.payload.get("support_classification")
                or claim.payload.get("claim_classification")
            )
            classification = _normalise_claim_classification(claim.payload)
            if raw_classification and raw_classification not in R3_CLAIM_CLASSIFICATIONS:
                add_error(
                    f"invalid_claim_classification:{claim.claim_id}:{raw_classification}",
                    claim.section_id,
                )
            claims_by_id[claim.claim_id] = claim
            if claim.section_id in claims_by_section:
                claims_by_section[claim.section_id].append(claim)

            claim_chunks = _unique(
                _list(claim.payload.get("supporting_text_chunk_ids"))
                + _list(claim.payload.get("supporting_chunk_ids"))
                + _list(claim.payload.get("context_text_chunk_ids"))
                + _list(claim.payload.get("contextual_support_chunk_ids"))
                + _list(claim.payload.get("factual_support_chunk_ids"))
            )
            for chunk_id in claim_chunks:
                if chunk_id not in chunk_ids:
                    add_error(f"invented_chunk_id:claim:{claim.claim_id}:{chunk_id}", claim.section_id)
                    continue
                record = _inventory_record(inventory, "chunks", chunk_id, claim.section_id)
                if record is not None and evidence_ceiling(record)[0] in {DISCOVERY, BACKGROUND}:
                    add_error(
                        f"discovery_only_cannot_support_claim:{claim.section_id}:{claim.claim_id}:{chunk_id}",
                        claim.section_id,
                    )
            for paper_id in _unique(
                _list(claim.payload.get("citation_paper_ids"))
                + _list(claim.payload.get("supporting_paper_ids"))
                + _list(claim.payload.get("paper_ids"))
            ):
                if paper_id not in paper_ids:
                    add_error(f"invented_paper_id:claim:{claim.claim_id}:{paper_id}", claim.section_id)
            for visual_id in _unique(claim.payload.get("supporting_visual_chunk_ids")):
                if visual_id not in visual_ids:
                    add_error(f"invented_visual_id:claim:{claim.claim_id}:{visual_id}", claim.section_id)

    gap_rows = [dict(item) for item in h.gaps if isinstance(item, Mapping)]
    gap_ids: set[str] = set()
    gaps_by_section: dict[str, list[dict[str, Any]]] = {sid: [] for sid in known_sections}
    for gap in gap_rows:
        gap_id = _text(gap.get("gap_id") or gap.get("request_id"))
        sid = _text(gap.get("section_id"))
        if not gap_id:
            add_error("gap_missing_id", sid)
        elif gap_id in gap_ids:
            add_error(f"duplicate_gap_id:{gap_id}", sid)
        else:
            gap_ids.add(gap_id)
        if sid not in known_sections:
            add_error(f"invented_section_id:gap:{gap_id}:{sid}")
        else:
            gaps_by_section[sid].append(gap)
        for claim_id in _unique(
            _list(gap.get("claim_ids"))
            + _list(gap.get("target_claim_ids"))
            + _list(gap.get("missing_claim_ids"))
        ):
            if claim_id not in claims_by_id:
                add_error(f"invented_claim_id:gap:{gap_id}:{claim_id}", sid)

    request_rows = [dict(item) for item in h.coverage_requests if isinstance(item, Mapping)]
    request_ids: set[str] = set()
    for request in request_rows:
        request_id = _text(request.get("request_id") or request.get("gap_id"))
        sid = _text(request.get("section_id"))
        if request_id:
            if request_id in request_ids:
                add_error(f"duplicate_coverage_request_id:{request_id}", sid)
            request_ids.add(request_id)
        else:
            add_error("coverage_request_missing_id", sid)
        if sid not in known_sections:
            add_error(f"invented_section_id:coverage_request:{request_id}:{sid}")
        for claim_id in _unique(
            _list(request.get("missing_claim_ids"))
            + _list(request.get("claim_ids"))
        ):
            if claim_id not in claims_by_id:
                add_error(f"invented_claim_id:coverage_request:{request_id}:{claim_id}", sid)

    for sid in sorted(known_sections):
        if not claims_by_section.get(sid) and not any(
            _lower(gap.get("kind") or gap.get("gap_type")) in {
                "missing_claim_decomposition",
                "missing_claims",
                "claim_inventory_missing",
            }
            or "missing_claim" in _lower(gap.get("reason"))
            for gap in gaps_by_section.get(sid, [])
        ):
            add_error(f"missing_claim_inventory:{sid}", sid)

    # Validate every claim/binding reference and its permission ceiling.
    for sid in sorted(known_sections):
        binding = h.material_bindings.get(sid)
        binding_claims = binding.claims if binding else {}
        expected_claims = {claim.claim_id for claim in claims_by_section.get(sid, [])}
        observed_claims = set(binding_claims)
        for claim_id in sorted(expected_claims - observed_claims):
            add_error(f"missing_material_binding_claim:{claim_id}", sid)
        for claim_id in sorted(observed_claims - expected_claims):
            add_error(f"invented_claim_id:material_binding:{sid}:{claim_id}", sid)

        for claim_id, raw_binding in sorted(binding_claims.items()):
            row = dict(raw_binding)
            ids = _binding_ids(row)
            declared_permission = _lower(row.get("permission_status"))
            write_status = _lower(row.get("write_status"))
            if declared_permission not in R3_PERMISSION_STATUSES:
                add_error(f"invalid_or_missing_permission_status:{sid}:{claim_id}", sid)
            if write_status not in R3_WRITE_STATUSES:
                add_error(f"invalid_or_missing_write_status:{sid}:{claim_id}", sid)
            for identifier in ids["papers"]:
                if identifier not in paper_ids:
                    add_error(f"invented_paper_id:material_binding:{sid}:{claim_id}:{identifier}", sid)
            for identifier in ids["supporting_chunks"] + ids["candidate_chunks"]:
                if identifier not in chunk_ids:
                    add_error(f"invented_chunk_id:material_binding:{sid}:{claim_id}:{identifier}", sid)
            for identifier in ids["visuals"]:
                if identifier not in visual_ids:
                    add_error(f"invented_visual_id:material_binding:{sid}:{claim_id}:{identifier}", sid)

            contextual_chunk_set: set[str] = set(ids["contextual_chunks"])
            ceilings: list[str] = []
            primary_ceilings: list[str] = []  # non-contextual supporting chunks only
            for chunk_id in ids["supporting_chunks"]:
                record = _inventory_record(inventory, "chunks", chunk_id, sid)
                if record is None:
                    continue
                ceiling, reason = evidence_ceiling(record)
                ceilings.append(ceiling)
                if chunk_id not in contextual_chunk_set:
                    primary_ceilings.append(ceiling)
                if ceiling in {DISCOVERY, BACKGROUND}:
                    add_error(
                        f"discovery_only_cannot_support_claim:{sid}:{claim_id}:{chunk_id}:{reason}",
                        sid,
                    )
            for chunk_id in ids["factual_chunks"]:
                record = _inventory_record(inventory, "chunks", chunk_id, sid)
                if record is not None and evidence_ceiling(record)[0] != FACTUAL:
                    add_error(f"invalid_factual_support_permission:{sid}:{claim_id}:{chunk_id}", sid)
            for chunk_id in ids["contextual_chunks"]:
                record = _inventory_record(inventory, "chunks", chunk_id, sid)
                if record is not None and evidence_ceiling(record)[0] not in {FACTUAL, QUALIFIED}:
                    add_error(f"invalid_contextual_support_permission:{sid}:{claim_id}:{chunk_id}", sid)

            if declared_permission == "bound" and (not ceilings or any(item != FACTUAL for item in ceilings)):
                add_error(f"bound_permission_requires_factual_support:{sid}:{claim_id}", sid)
            if declared_permission == "qualified_only" and (not ceilings or any(item not in {FACTUAL, QUALIFIED} for item in ceilings)):
                add_error(f"qualified_permission_has_no_acceptable_support:{sid}:{claim_id}", sid)
            if not ceilings and declared_permission in {"bound", "qualified_only"}:
                add_error(f"support_ids_missing_for_permission:{sid}:{claim_id}", sid)
            # Use primary (non-contextual) ceilings here: contextual background
            # chunks on an open_question claim must not conflict with unbound.
            if primary_ceilings and declared_permission in {"unbound", "unresolved", "needs_more_literature"}:
                add_error(f"permission_status_conflicts_with_support:{sid}:{claim_id}", sid)

            claim = claims_by_id.get(claim_id)
            if claim is None:
                continue
            claim_classification = _normalise_claim_classification(
                claim.payload,
                row,
            )
            binding_classification = _lower(
                row.get("support_classification")
                or row.get("claim_classification")
            )
            if binding_classification and binding_classification not in R3_CLAIM_CLASSIFICATIONS:
                add_error(
                    f"invalid_binding_classification:{sid}:{claim_id}:{binding_classification}",
                    sid,
                )
            if (
                binding_classification
                and binding_classification != claim_classification
                and not (
                    binding_classification == "qualified"
                    and claim_classification == "supported"
                )
            ):
                add_error(
                    f"claim_binding_classification_mismatch:{sid}:{claim_id}",
                    sid,
                )
            # Use only non-contextual (primary) supporting chunks to check factual
            # support.  Contextual chunks legitimately carry a QUALIFIED ceiling and
            # must not downgrade a claim whose direct evidence is fully factual.
            _factual_check_ceilings = primary_ceilings if primary_ceilings else ceilings
            if claim_classification == "supported" and (
                not _factual_check_ceilings
                or any(item != FACTUAL for item in _factual_check_ceilings)
            ):
                add_error(
                    f"supported_claim_requires_factual_support:{sid}:{claim_id}",
                    sid,
                )
            if claim_classification == "qualified" and not ceilings:
                add_error(
                    f"qualified_claim_requires_permission_eligible_support:{sid}:{claim_id}",
                    sid,
                )
            unresolved = _explicit_unresolved(claim, row)
            if claim.criticality == "load_bearing":
                if not ceilings and not unresolved:
                    add_error(
                        f"load_bearing_claim_without_support_or_unresolved_status:{sid}:{claim_id}",
                        sid,
                    )
                elif not ceilings and unresolved and not _has_unresolved_reason(claim, row):
                    add_error(f"unresolved_load_bearing_claim_missing_reason:{sid}:{claim_id}", sid)
            # ``permission_status`` is an evidence ceiling, not a command to
            # write at the strongest possible certainty.  Fully factual
            # material may legitimately support a deliberately qualified
            # claim (for example when only part of a compound statement is
            # covered).  The unsafe direction is the reverse: a binding must
            # never claim a stronger write status than its evidence permits.
            # Derive the effective permission from primary (non-contextual)
            # evidence: if all primary supporting chunks are factual, the agent
            # may write at "bound" strength even when it conservatively declared
            # a lower permission_status (e.g. because contextual chunks are QUALIFIED).
            _primary_all_factual = bool(primary_ceilings) and all(
                c == FACTUAL for c in primary_ceilings
            )
            _effective_write_perm = "bound" if _primary_all_factual else declared_permission
            if write_status == "bound" and _effective_write_perm != "bound":
                add_error(f"write_status_permission_mismatch:{sid}:{claim_id}", sid)
            if (
                write_status == "write_with_qualified_support"
                and declared_permission not in {"bound", "qualified_only"}
            ):
                add_error(f"write_status_permission_mismatch:{sid}:{claim_id}", sid)

    # Section-level synthesis and references.
    for sid, bundle_obj in h.synthesis_bundles.items():
        bundle = bundle_obj.payload
        if _text(bundle.get("section_id")) != sid:
            add_error(f"synthesis_bundle_section_mismatch:{sid}", sid)
        for claim_id in _unique(
            item.get("claim_id")
            for item in _list(bundle.get("claim_category_assignments"))
            if isinstance(item, Mapping)
        ):
            if claim_id not in claims_by_id:
                add_error(f"invented_claim_id:synthesis_bundle:{sid}:{claim_id}", sid)
        for paper_id in _unique(bundle.get("paper_ids")):
            if paper_id not in paper_ids:
                add_error(f"invented_paper_id:synthesis_bundle:{sid}:{paper_id}", sid)
        for chunk_id in _unique(bundle.get("chunk_ids")):
            if chunk_id not in chunk_ids:
                add_error(f"invented_chunk_id:synthesis_bundle:{sid}:{chunk_id}", sid)
        for visual_id in _unique(bundle.get("visual_chunk_ids") or bundle.get("visual_asset_ids")):
            if visual_id not in visual_ids:
                add_error(f"invented_visual_id:synthesis_bundle:{sid}:{visual_id}", sid)

    relation_edges = _edge_rows(h.relation_graph)
    seen_edge_ids: set[str] = set()
    for edge in relation_edges:
        edge_id = _text(edge.get("edge_id"))
        source = _text(edge.get("source_paper_id") or edge.get("source_id"))
        target = _text(edge.get("target_paper_id") or edge.get("target_id"))
        sid = _text(edge.get("section_id"))
        if edge_id:
            if edge_id in seen_edge_ids:
                add_error(f"duplicate_relation_edge_id:{edge_id}", sid)
            seen_edge_ids.add(edge_id)
        else:
            add_error("relation_edge_missing_id", sid)
        if source not in paper_ids:
            add_error(f"invented_paper_id:relation_source:{edge_id}:{source}", sid)
        if target not in paper_ids:
            add_error(f"invented_paper_id:relation_target:{edge_id}:{target}", sid)
        basis = _basis_ids(edge)
        if not basis:
            add_error(f"relation_requires_basis_chunks:{edge_id or source + '->' + target}", sid)
        for chunk_id in basis:
            if chunk_id not in chunk_ids:
                add_error(f"invented_chunk_id:relation_basis:{edge_id}:{chunk_id}", sid)
            else:
                record = _inventory_record(inventory, "chunks", chunk_id, sid)
                if record is not None and evidence_ceiling(record)[0] in {DISCOVERY, BACKGROUND}:
                    add_error(f"relation_basis_not_evidence_eligible:{edge_id}:{chunk_id}", sid)
        if sid and sid not in known_sections:
            add_error(f"invented_section_id:relation:{edge_id}:{sid}")

    dag_edges = _edge_rows(h.claim_dag)
    seen_dag_ids: set[str] = set()
    for edge in dag_edges:
        edge_id = _text(edge.get("edge_id"))
        source = _text(edge.get("source_claim_id") or edge.get("source_id"))
        target = _text(edge.get("target_claim_id") or edge.get("target_id"))
        sid = _text(edge.get("source_section_id") or edge.get("section_id"))
        if edge_id:
            if edge_id in seen_dag_ids:
                add_error(f"duplicate_claim_dag_edge_id:{edge_id}", sid)
            seen_dag_ids.add(edge_id)
        else:
            add_error("claim_dag_edge_missing_id", sid)
        if source not in claims_by_id:
            add_error(f"invented_claim_id:dag_source:{edge_id}:{source}", sid)
        if target not in claims_by_id:
            add_error(f"invented_claim_id:dag_target:{edge_id}:{target}", sid)
        if source and source == target:
            add_error(f"claim_dag_self_edge:{edge_id or source}", sid)
        relation_type = _lower(edge.get("relation_type") or edge.get("edge_type"))
        if relation_type and relation_type not in R3_ALLOWED_DAG_RELATIONS:
            add_error(f"invalid_claim_dag_relation_type:{edge_id}:{relation_type}", sid)
        basis = _basis_ids(edge)
        if not basis:
            add_error(f"claim_dag_relation_requires_basis_chunks:{edge_id or source + '->' + target}", sid)
        for chunk_id in basis:
            if chunk_id not in chunk_ids:
                add_error(f"invented_chunk_id:claim_dag_basis:{edge_id}:{chunk_id}", sid)
            else:
                record = _inventory_record(inventory, "chunks", chunk_id, sid)
                if record is not None and evidence_ceiling(record)[0] in {DISCOVERY, BACKGROUND}:
                    add_error(f"claim_dag_basis_not_evidence_eligible:{edge_id}:{chunk_id}", sid)
    if _topological_cycle(dag_edges):
        add_error("claim_dag_cycle_detected")

    visual_binding_ids: set[str] = set()
    for sid in sorted(known_sections):
        for binding in h.visual_bindings.get(sid, []):
            visual_id = _text(binding.get("visual_id") or binding.get("visual_chunk_id") or binding.get("visual_asset_id"))
            if not visual_id:
                add_error("visual_binding_missing_id", sid)
                continue
            if visual_id in visual_binding_ids:
                add_warning(f"visual_bound_to_multiple_sections:{visual_id}", sid)
            visual_binding_ids.add(visual_id)
            if visual_id not in visual_ids:
                add_error(f"invented_visual_id:visual_binding:{sid}:{visual_id}", sid)
            claim_id = _text(binding.get("claim_id"))
            if claim_id and claim_id not in claims_by_id:
                add_error(f"invented_claim_id:visual_binding:{sid}:{claim_id}", sid)
        seen_need_ids: set[str] = set()
        for need in h.visual_needs.get(sid, []):
            need_id = _text(need.get("need_id") or need.get("visual_need_id"))
            if not need_id:
                add_error("visual_need_missing_id", sid)
            elif need_id in seen_need_ids:
                add_error(f"duplicate_visual_need_id:{need_id}", sid)
            seen_need_ids.add(need_id)

        atlas_row = next(
            (row for row in h.coverage_atlas.sections if _text(row.get("section_id")) == sid),
            {},
        )
        relation_coverage = _dict(atlas_row.get("relationship_coverage"))
        for task in _list(relation_coverage.get("missing_semantic_relation_tasks")):
            if _text(task):
                add_warning(f"missing_relation_task:{sid}:{_text(task)}", sid)

    # Compact, deterministic authoring readiness.  Structural errors block
    # immediately; scientific gaps belong to their section and do not revoke
    # otherwise valid sections from the same handoff.
    section_readiness: dict[str, dict[str, Any]] = {}
    for sid in section_ids:
        blockers = list(dict.fromkeys(section_errors.get(sid, [])))
        limits: list[str] = []
        unresolved_claim_ids = sorted(
            claim.claim_id
            for claim in claims_by_section.get(sid, [])
            if claim.criticality == "load_bearing"
            and _normalise_claim_classification(
                claim.payload,
                h.material_bindings.get(sid, {}).claims.get(claim.claim_id)
                if h.material_bindings.get(sid)
                else {},
            ) == "open_question"
        )
        binding_obj = h.material_bindings.get(sid)
        binding_payload = binding_obj.payload if binding_obj else {}
        bundle_obj = h.synthesis_bundles.get(sid)
        bundle_payload = bundle_obj.payload if bundle_obj else {}
        declared_outcome = _lower(
            binding_payload.get("section_outcome")
            or bundle_payload.get("section_outcome")
            or binding_payload.get("outcome")
            or bundle_payload.get("outcome")
        )
        if declared_outcome not in R3_SECTION_OUTCOMES:
            declared_outcome = ""

        load_open_claims: list[str] = []
        qualified_claims: list[str] = []
        optional_open_claims: list[str] = []
        for claim in claims_by_section.get(sid, []):
            raw_binding = binding_obj.claims.get(claim.claim_id, {}) if binding_obj else {}
            classification = _normalise_claim_classification(claim.payload, raw_binding)
            if classification == "qualified":
                qualified_claims.append(claim.claim_id)
            if classification == "open_question":
                if claim.criticality == "load_bearing":
                    load_open_claims.append(claim.claim_id)
                else:
                    optional_open_claims.append(claim.claim_id)
        if qualified_claims:
            limits.extend("qualified_claim:" + item for item in sorted(qualified_claims))
        if optional_open_claims:
            limits.extend("open_optional_claim:" + item for item in sorted(optional_open_claims))
        required_visual_missing: list[str] = []
        bound_visuals = {
            _text(row.get("visual_id") or row.get("visual_chunk_id"))
            for row in h.visual_bindings.get(sid, [])
        }
        for need in h.visual_needs.get(sid, []):
            if bool(need.get("required")) and not (
                _text(need.get("visual_id")) in bound_visuals
                or bound_visuals
                and bool(need.get("satisfied"))
            ):
                required_visual_missing.append(_text(need.get("need_id")))
        if required_visual_missing:
            limits.append(
                "required_visual_needs_unbound:" + ",".join(sorted(required_visual_missing))
            )
        contract = h.section_argument_contracts.get(sid)
        contract_status = _lower(contract.payload.get("status")) if contract else "missing"
        if contract_status not in {"contract_ready", "ready", ""}:
            blockers.append(f"section_contract_not_ready:{contract_status}")
        if not claims_by_section.get(sid):
            blockers.append("section_has_no_claims")
        blocking_gaps = [
            _text(gap.get("gap_id") or gap.get("request_id"))
            for gap in gaps_by_section.get(sid, [])
            if bool(gap.get("blocking"))
        ]
        limits.extend("declared_gap:" + gap_id for gap_id in sorted(item for item in blocking_gaps if item))
        limits.extend(
            "open_question:" + claim_id
            for claim_id in sorted(load_open_claims)
        )
        # A section with a usable authorable backbone is admitted with limits;
        # unresolved load-bearing claims remain visible for later retrieval but
        # do not invalidate all other claims in the section.  A section whose
        # entire claim set is unresolved still remains closed.
        authorable_claim_ids = {
            claim.claim_id
            for claim in claims_by_section.get(sid, [])
            if _normalise_claim_classification(
                claim.payload,
                binding_obj.claims.get(claim.claim_id, {}) if binding_obj else {},
            ) != "open_question"
            and _binding_ids(
                binding_obj.claims.get(claim.claim_id, {}) if binding_obj else {}
            )["supporting_chunks"]
        }
        blockers = _compact_validation_messages(list(dict.fromkeys(blockers)))
        limits = _compact_validation_messages(list(dict.fromkeys(limits)))
        if blockers:
            status = "blocked" if any(
                item.startswith((
                    "missing_", "stale_", "invented_", "duplicate_", "invalid_",
                    "discovery_", "relation_", "claim_dag_", "bound_", "qualified_",
                    "support_", "load_bearing_", "unresolved_load_bearing_",
                    "required_visual_", "section_contract_not_ready",
                ))
                for item in blockers
            ) else "needs_more_literature"
            outcome = "needs_more_literature"
        elif (load_open_claims or unresolved_claim_ids) and not authorable_claim_ids:
            merge = _dict(
                bundle_payload.get("merge_recommendation")
                or binding_payload.get("merge_recommendation")
            )
            outcome = "merge_required" if merge else "needs_more_literature"
            status = "merge_required" if outcome == "merge_required" else "needs_more_literature"
        elif load_open_claims or unresolved_claim_ids:
            limits.extend(
                "unresolved_load_bearing_claim:" + claim_id
                for claim_id in sorted(set(load_open_claims) | set(unresolved_claim_ids))
            )
            outcome = "ready_with_limits"
            status = "ready_with_limits"
        elif declared_outcome:
            outcome = declared_outcome
            # A producer-declared ``ready`` status cannot erase a separately
            # declared limit such as an unbound required visual contract.
            # Preserve the core handoff while carrying that limit forward.
            if outcome == "ready" and limits:
                outcome = "ready_with_limits"
            status = {
                "ready": "ready_for_authoring",
                "ready_with_limits": "ready_with_limits",
                "merge_required": "merge_required",
                "needs_more_literature": "needs_more_literature",
            }[outcome]
        else:
            outcome = "ready_with_limits" if limits else "ready"
            status = "ready_for_authoring" if outcome == "ready" else "ready_with_limits"
        if outcome == "merge_required" and not any(
            item.startswith("merge_required") for item in limits
        ):
            limits.append("merge_required")
        ready_for_authoring = outcome in {"ready", "ready_with_limits"} and not blockers
        section_readiness[sid] = {
            "section_id": sid,
            "status": status,
            "outcome": outcome,
            "ready_for_authoring": ready_for_authoring,
            "blocking_reasons": blockers,
            "unresolved_load_bearing_claim_ids": unresolved_claim_ids,
            "required_visual_needs_unbound": sorted(required_visual_missing),
            "declared_limits": limits,
            "blocking_gap_ids": sorted(item for item in blocking_gaps if item),
            "warnings": _compact_validation_messages(section_warnings.get(sid, [])),
        }

    if h.legacy_migration:
        add_warning("legacy_migration_path_used")
    raw_error_count = len(errors)
    compact_errors = _compact_validation_messages(errors)
    compact_warnings = _compact_validation_messages(warnings)
    if errors:
        global_status = "blocked"
    elif section_readiness and all(
        item.get("outcome") == "ready" for item in section_readiness.values()
    ):
        global_status = "ready_for_authoring"
    elif any(item.get("ready_for_authoring") for item in section_readiness.values()):
        global_status = "ready_with_limits"
    else:
        global_status = "needs_more_literature"
    global_readiness = {
        "status": global_status,
        "ready_for_authoring": bool(
            any(item.get("ready_for_authoring") for item in section_readiness.values())
            and not errors
        ),
        "partial_handoff_allowed": bool(
            any(item.get("ready_for_authoring") for item in section_readiness.values())
            and not errors
        ),
        "section_count": len(section_ids),
        "ready_section_ids": sorted(
            sid for sid, value in section_readiness.items() if value.get("ready_for_authoring")
        ),
        "not_ready_section_ids": sorted(
            sid for sid, value in section_readiness.items() if not value.get("ready_for_authoring")
        ),
        "section_outcomes": {
            sid: value.get("outcome") for sid, value in sorted(section_readiness.items())
        },
        "validation_status": "passed" if not errors else "failed",
        "blocking_error_count": len(compact_errors),
        "raw_blocking_error_count": raw_error_count,
    }
    return R3ValidationReport(
        valid=not errors,
        errors=compact_errors,
        warnings=compact_warnings,
        section_readiness=section_readiness,
        global_readiness=global_readiness,
    )


def _permission_fields(raw: Any) -> dict[str, Any]:
    row = _dict(raw)
    return {
        key: row.get(key)
        for key in (
            "use_permission",
            "scope_fit",
            "content_depth",
            "context_complete",
            "source_kind",
            "allowed_claim_kinds",
            "retrieval_role",
        )
        if key in row
    }


def _asset_to_dict(asset: Any, identifier: str, kind: str) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    if isinstance(asset, Mapping):
        raw = dict(asset)
    elif hasattr(asset, "__dataclass_fields__"):
        raw = {
            name: getattr(asset, name)
            for name in asset.__dataclass_fields__
            if hasattr(asset, name)
        }
    else:
        for name in (
            "paper_id", "chunk_id", "visual_id", "scope_fit", "use_permission",
            "content_depth", "context_complete", "source_kind", "paper_id",
            "allowed_claim_kinds", "status", "kind", "argument_type",
            "argument_claim", "caption", "local_image_path",
        ):
            if hasattr(asset, name):
                raw[name] = getattr(asset, name)
    id_key = {"papers": "paper_id", "chunks": "chunk_id", "visuals": "visual_id"}[kind]
    raw[id_key] = _text(raw.get(id_key) or identifier)
    if kind == "chunks":
        raw.setdefault("paper_id", _text(raw.get("paper_id")))
    if kind == "visuals":
        raw.setdefault("status", "unknown")
    return _as_serializable(raw)


def _merge_inventory_record(
    inventory: dict[str, dict[str, dict[str, Any]]],
    kind: str,
    identifier: str,
    record: Mapping[str, Any],
    section_id: str,
) -> None:
    if not identifier:
        return
    target = inventory.setdefault(kind, {})
    current = target.setdefault(identifier, {"id": identifier})
    id_key = {"papers": "paper_id", "chunks": "chunk_id", "visuals": "visual_id"}[kind]
    current[id_key] = identifier
    for key, value in _as_serializable(dict(record)).items():
        if key in {"permissions_by_section", "section_ids", "id"}:
            continue
        if value not in (None, "", [], {}, ()):  # preserve the first stable value otherwise
            current.setdefault(key, value)
    section_ids = _unique(current.get("section_ids") or [])
    if section_id:
        section_ids.append(section_id)
    current["section_ids"] = _unique(section_ids)
    permissions = current.setdefault("permissions_by_section", {})
    if section_id:
        section_row = permissions.setdefault(section_id, {})
        for key, value in _permission_fields(record).items():
            if value not in (None, ""):
                section_row[key] = _as_serializable(value)


def _normalise_state_value(state: Any) -> dict[str, Any]:
    if isinstance(state, Mapping):
        return dict(state)
    return {}


def build_r3_production_handoff(
    *,
    topic_identity: Mapping[str, Any] | None,
    sections: Iterable[Mapping[str, Any]],
    coverage_atlas: Mapping[str, Any],
    section_argument_contracts: Mapping[str, Any],
    claims_by_criticality: Mapping[str, Any],
    material_inventory: Mapping[str, Any],
    material_bindings: Mapping[str, Any],
    relation_graph: Mapping[str, Any] | None = None,
    claim_dag: Mapping[str, Any] | None = None,
    gaps: Iterable[Mapping[str, Any]] = (),
    coverage_requests: Iterable[Mapping[str, Any]] = (),
    synthesis_bundles: Mapping[str, Any] | Iterable[Mapping[str, Any]] = (),
    visual_bindings: Mapping[str, Any] | Iterable[Mapping[str, Any]] = (),
    visual_needs: Mapping[str, Any] | Iterable[Mapping[str, Any]] = (),
    source_artifacts: Mapping[str, Any] | None = None,
    legacy_migration: Mapping[str, Any] | None = None,
) -> R3ProductionHandoff:
    """Construct a canonical handoff from already normalized components."""

    section_rows = [dict(item) for item in sections if isinstance(item, Mapping)]
    section_ids = _unique(item.get("section_id") for item in section_rows)
    contract_rows = _normalise_contracts(section_argument_contracts)
    contracts = {
        sid: R3SectionArgumentContract.from_dict(contract_rows.get(sid, {}), sid)
        for sid in contract_rows
    }
    grouped: dict[str, list[R3Claim]] = {key: [] for key in R3_CRITICALITIES}
    for criticality, values in _dict(claims_by_criticality).items():
        normalized = _lower(criticality)
        for raw in _claim_rows(values):
            claim = R3Claim.from_dict(raw, criticality=normalized)
            grouped.setdefault(claim.criticality, []).append(claim)
    inventory = {
        "papers": _records_map(_dict(material_inventory).get("papers"), "paper_id"),
        "chunks": _records_map(_dict(material_inventory).get("chunks"), "chunk_id"),
        "visuals": _records_map(
            _dict(material_inventory).get("visuals") or _dict(material_inventory).get("visual_chunks"),
            "visual_id",
        ),
    }
    binding_rows = _normalise_bindings(material_bindings)
    bindings = {
        sid: R3MaterialBinding.from_dict(value, sid)
        for sid, value in binding_rows.items()
    }
    # Bindings carry the final permission ceiling.  When a legacy/direct
    # caller omitted the newer classification field, derive the conservative
    # label from that binding once; this is normalization, not evidence
    # discovery or claim strengthening.
    for claim in [item for rows in grouped.values() for item in rows]:
        if not _lower(
            claim.payload.get("support_classification")
            or claim.payload.get("claim_classification")
        ):
            binding = bindings.get(claim.section_id)
            raw_binding = binding.claims.get(claim.claim_id, {}) if binding else {}
            classification = _normalise_claim_classification(claim.payload, raw_binding)
            claim.payload["support_classification"] = classification
            claim.payload.setdefault("claim_classification", classification)
    bundle_rows = _normalise_bundles(synthesis_bundles)
    bundles = {
        sid: R3CompactSynthesisBundle.from_dict(value, sid)
        for sid, value in bundle_rows.items()
    }
    request_rows = [dict(item) for item in _list(coverage_requests) if isinstance(item, Mapping)]
    visual_binding_map = _normalise_visual_map(visual_bindings, section_ids)
    visual_need_map = _normalise_visual_map(visual_needs, section_ids)
    handoff = R3ProductionHandoff(
        schema_version=R3_HANDOFF_SCHEMA_VERSION,
        handoff_version=R3_HANDOFF_VERSION,
        handoff_kind=R3_HANDOFF_KIND,
        topic_identity=_dict(topic_identity),
        sections=section_rows,
        coverage_atlas=CoverageAtlas.from_dict(coverage_atlas),
        section_argument_contracts=contracts,
        claims_by_criticality=grouped,
        material_inventory=inventory,
        material_bindings=bindings,
        relation_graph=_dict(relation_graph) or {"edges": []},
        claim_dag=_dict(claim_dag) or {"edges": []},
        gaps=[dict(item) for item in _list(gaps) if isinstance(item, Mapping)],
        coverage_requests=request_rows,
        synthesis_bundles=bundles,
        visual_bindings=visual_binding_map,
        visual_needs=visual_need_map,
        source_artifacts=_dict(source_artifacts),
        legacy_migration=_dict(legacy_migration),
    )
    report = handoff.validate()
    handoff.readiness = {
        "sections": report.section_readiness,
        "global": report.global_readiness,
        "validation": report.to_dict(),
    }
    return handoff


def build_r3_production_handoff_from_phase3(
    *,
    blueprint: Mapping[str, Any],
    states: Iterable[Mapping[str, Any]],
    coverage_atlas: Mapping[str, Any],
    claim_graph: Mapping[str, Any],
    relation_graph: Mapping[str, Any],
    coverage_requests: Iterable[Any] = (),
    phase_run: Mapping[str, Any] | None = None,
    acceptance: Mapping[str, Any] | None = None,
    output_dir: Path | None = None,
) -> R3ProductionHandoff:
    """Producer adapter for the in-memory outputs of ``Phase3ArgumentOrchestrator``.

    The adapter only accepts IDs from each state's canonical asset graph or
    records.  It never promotes IDs found solely in a claim, binding, or
    bundle into the inventory; such IDs remain validation errors.
    """

    blueprint_row = _dict(blueprint)
    raw_sections = [
        dict(item) for item in _list(blueprint_row.get("sections")) if isinstance(item, Mapping)
    ]
    states_by_id = {
        _text(_normalise_state_value(state).get("section", {}).get("section_id")): _normalise_state_value(state)
        for state in states
        if _text(_normalise_state_value(state).get("section", {}).get("section_id"))
    }
    section_ids = _unique(item.get("section_id") for item in raw_sections)
    for sid in states_by_id:
        if sid not in section_ids:
            section_ids.append(sid)
    section_rows: list[dict[str, Any]] = []
    for sid in section_ids:
        source = next((item for item in raw_sections if _text(item.get("section_id")) == sid), {})
        row = {
            "section_id": sid,
            "title": _text(source.get("title") or source.get("section_title")),
            "argument_role": _text(source.get("argument_role") or source.get("chapter_argument")),
        }
        if _dict(source.get("topic_identity")):
            row["topic_identity"] = _dict(source.get("topic_identity"))
        if _text(source.get("topic_id")):
            row["topic_id"] = _text(source.get("topic_id"))
        runtime_failure = _dict(states_by_id.get(sid, {}).get("runtime_failure"))
        if runtime_failure:
            row["runtime_status"] = "failed"
            row["runtime_failure"] = runtime_failure
        section_rows.append(row)

    atlas = _dict(coverage_atlas)
    atlas_rows = _section_map(atlas.get("sections"))
    atlas_sections = []
    for sid in section_ids:
        atlas_sections.append(dict(atlas_rows.get(sid, {
            "section_id": sid,
            "status": "unprocessed",
            "needs_expansion": True,
            "discovery_status": {"complete": False, "reason": "section_not_processed"},
        })))
    atlas = dict(atlas)
    atlas["sections"] = atlas_sections
    atlas["section_count"] = len(atlas_sections)

    contracts: dict[str, dict[str, Any]] = {}
    claims_by: dict[str, list[dict[str, Any]]] = {key: [] for key in R3_CRITICALITIES}
    material_bindings: dict[str, dict[str, Any]] = {}
    bundles: dict[str, dict[str, Any]] = {}
    inventory: dict[str, dict[str, dict[str, Any]]] = {"papers": {}, "chunks": {}, "visuals": {}}
    visual_bindings: dict[str, list[dict[str, Any]]] = {sid: [] for sid in section_ids}
    visual_needs: dict[str, list[dict[str, Any]]] = {sid: [] for sid in section_ids}
    gaps: list[dict[str, Any]] = []

    for sid in section_ids:
        state = states_by_id.get(sid, {})
        section = _dict(state.get("section"))
        contract = state.get("contract")
        if contract is not None and hasattr(contract, "to_dict"):
            contract_payload = _dict(contract.to_dict())
        else:
            contract_payload = _dict(section.get("section_contract") or section.get("section_argument_contract"))
        if not contract_payload:
            contract_payload = {
                "schema_version": "research_harness.section_argument_contract.v1",
                "section_id": sid,
                "status": "unprocessed",
                "unresolved_items": ["section_not_processed"],
            }
        contract_payload["section_id"] = sid
        contracts[sid] = contract_payload

        claims = [dict(item) for item in _list(state.get("claims") or section.get("claims")) if isinstance(item, Mapping)]
        for claim in claims:
            claim["section_id"] = _text(claim.get("section_id") or sid)
            criticality = _normalise_criticality(claim)
            claim["criticality"] = criticality
            claims_by.setdefault(criticality, []).append(claim)

        raw_binding = state.get("bindings") or {}
        if hasattr(raw_binding, "to_dict"):
            raw_binding = raw_binding.to_dict()
        binding_payload = dict(_dict(raw_binding))
        binding_payload["section_id"] = sid
        raw_claim_bindings = _dict(binding_payload.get("claims"))
        binding_payload["claims"] = {
            _text(claim_id): dict(binding)
            for claim_id, binding in raw_claim_bindings.items()
            if _text(claim_id) and isinstance(binding, Mapping)
        }
        material_bindings[sid] = binding_payload

        bundle = state.get("bundle") or {}
        if hasattr(bundle, "to_dict"):
            bundle = bundle.to_dict()
        bundle_payload = dict(_dict(bundle))
        bundle_payload["section_id"] = sid
        bundles[sid] = bundle_payload

        runtime_failure = _dict(state.get("runtime_failure"))
        if runtime_failure:
            gaps.append({
                "gap_id": f"gap:{sid}:runtime_failure",
                "section_id": sid,
                "kind": "runtime_failure",
                "blocking": True,
                "priority": "engineering",
                "status": "failed",
                "reason": _text(
                    runtime_failure.get("reason")
                    or "section coverage worker failed"
                ),
                "source": "section_coverage_manifest",
            })

        graph = state.get("graph")
        graph_papers = getattr(graph, "papers", {}) if graph is not None else {}
        graph_chunks = getattr(graph, "chunks", {}) if graph is not None else {}
        graph_visuals = getattr(graph, "visuals", {}) if graph is not None else {}
        for identifier, asset in _dict(graph_papers).items():
            _merge_inventory_record(inventory, "papers", _text(identifier), _asset_to_dict(asset, _text(identifier), "papers"), sid)
        for identifier, asset in _dict(graph_chunks).items():
            _merge_inventory_record(inventory, "chunks", _text(identifier), _asset_to_dict(asset, _text(identifier), "chunks"), sid)
        for identifier, asset in _dict(graph_visuals).items():
            _merge_inventory_record(inventory, "visuals", _text(identifier), _asset_to_dict(asset, _text(identifier), "visuals"), sid)
        for record in _list(state.get("records")):
            if not isinstance(record, Mapping):
                continue
            chunk_id = _text(record.get("chunk_id"))
            if chunk_id:
                _merge_inventory_record(inventory, "chunks", chunk_id, record, sid)

        # Preserve explicit visual contracts from the producer state before
        # deriving compatibility bindings from claims/bundles.  A visual need
        # is a contract, not proof that a visual asset exists.
        provided_visual_bindings = _normalise_visual_map(
            state.get("visual_bindings") or section.get("visual_bindings"),
            [sid],
        ).get(sid, [])
        for raw_visual in provided_visual_bindings:
            visual = dict(raw_visual)
            visual["section_id"] = sid
            visual.setdefault(
                "visual_binding_id",
                f"{sid}:visual:{len(visual_bindings[sid]) + 1:02d}",
            )
            visual_bindings[sid].append(visual)
        provided_visual_needs = _normalise_visual_map(
            state.get("visual_needs") or section.get("visual_needs"),
            [sid],
        ).get(sid, [])
        for raw_need in provided_visual_needs:
            need = dict(raw_need)
            need["section_id"] = sid
            need.setdefault(
                "need_id",
                _text(need.get("visual_need_id"))
                or f"{sid}:visual_need:{len(visual_needs[sid]) + 1:02d}",
            )
            visual_needs[sid].append(need)

        explicit_visual_ids: list[tuple[str, str]] = []
        for claim in claims:
            for visual_id in _unique(claim.get("supporting_visual_chunk_ids")):
                explicit_visual_ids.append((visual_id, _text(claim.get("claim_id"))))
        for visual_id in _unique(
            bundle_payload.get("visual_chunk_ids")
            or bundle_payload.get("visual_asset_ids")
            or binding_payload.get("visual_chunk_ids")
        ):
            explicit_visual_ids.append((visual_id, ""))
        seen_visuals: set[str] = set()
        for visual_id, claim_id in explicit_visual_ids:
            if visual_id in seen_visuals:
                continue
            seen_visuals.add(visual_id)
            visual_bindings[sid].append({
                "visual_binding_id": f"{sid}:visual:{len(visual_bindings[sid]) + 1:02d}",
                "section_id": sid,
                "visual_id": visual_id,
                "claim_id": claim_id,
                "status": "bound",
                "source": "phase3_claim_or_bundle",
            })

        slots = _list(contract_payload.get("visual_argument_slots") or section.get("visual_argument_slots"))
        requirements = _dict(section.get("visual_requirements"))
        if not slots and requirements.get("required"):
            slots = [{"slot_id": f"{sid}:visual_slot:01", "required": True, "purpose": "section_visual"}]
        for index, slot in enumerate(slots, start=1):
            raw_slot = dict(slot) if isinstance(slot, Mapping) else {"purpose": _text(slot)}
            need_id = _text(raw_slot.get("need_id") or raw_slot.get("slot_id") or f"{sid}:visual_need:{index:02d}")
            if any(_text(item.get("need_id")) == need_id for item in visual_needs[sid]):
                continue
            visual_needs[sid].append({
                "need_id": need_id,
                "section_id": sid,
                "purpose": _text(raw_slot.get("purpose") or raw_slot.get("description")),
                "required": bool(raw_slot.get("required", requirements.get("required", False))),
                "claim_id": _text(raw_slot.get("claim_id")),
                "satisfied": False,
            })

        if not claims:
            gaps.append({
                "gap_id": f"gap:{sid}:missing_claims",
                "section_id": sid,
                "kind": "missing_claim_decomposition",
                "blocking": True,
                "reason": "phase3_produced_no_valid_claims",
            })
        binding_claims = _dict(binding_payload.get("claims"))
        for claim_id, binding in binding_claims.items():
            claim = next(
                (claim for claim in claims if _text(claim.get("claim_id")) == claim_id),
                {"importance": binding.get("importance", "supporting")},
            )
            importance = _normalise_criticality(claim)
            classification = _normalise_claim_classification(claim, binding)
            missing_components = _list(binding.get("missing_evidence_components"))
            write_status = _lower(binding.get("write_status"))
            has_gap = bool(
                classification == "open_question"
                or not _binding_ids(binding)["supporting_chunks"]
                or write_status in {"needs_more_literature", "unresolved", "write_with_declared_gap"}
                or missing_components
            )
            if has_gap:
                is_load_bearing_gap = importance == "load_bearing" and classification == "open_question"
                gaps.append({
                    "gap_id": f"gap:{sid}:{claim_id}",
                    "section_id": sid,
                    "kind": (
                        "load_bearing_claim_material_gap"
                        if importance == "load_bearing"
                        else "optional_claim_material_gap"
                    ),
                    "claim_ids": [claim_id],
                    "missing_components": [
                        _text(item) for item in missing_components if _text(item)
                    ],
                    "classification": classification,
                    "adaptation_action": _text(binding.get("adaptation_action")),
                    "blocking": is_load_bearing_gap,
                    "priority": "load_bearing" if is_load_bearing_gap else "optional",
                    "status": "unresolved",
                })
        merge_recommendation = _dict(
            bundle_payload.get("merge_recommendation")
            or binding_payload.get("merge_recommendation")
        )
        if merge_recommendation:
            gaps.append({
                "gap_id": f"gap:{sid}:merge_required",
                "section_id": sid,
                "kind": "merge_recommendation",
                "target_section_ids": _unique(
                    merge_recommendation.get("target_section_ids")
                    or merge_recommendation.get("section_ids")
                ),
                "reason": _text(merge_recommendation.get("reason")),
                "blocking": True,
                "priority": "load_bearing",
                "status": "pending",
            })

    requests: list[dict[str, Any]] = []
    for request in _list(coverage_requests):
        if hasattr(request, "to_dict"):
            request = request.to_dict()
        if isinstance(request, Mapping):
            row = dict(request)
            requests.append(row)
            sid = _text(row.get("section_id"))
            request_id = _text(row.get("request_id"))
            gaps.append({
                "gap_id": request_id or f"gap:{sid}:coverage_request:{len(gaps) + 1:02d}",
                "request_id": request_id,
                "section_id": sid,
                "kind": "coverage_request",
                "claim_ids": _unique(row.get("missing_claim_ids")),
                "missing_roles": _unique(row.get("missing_roles")),
                "missing_relation_tasks": _unique(row.get("missing_relation_tasks")),
                "blocking": _lower(row.get("priority")) == "load_bearing",
                "priority": _text(row.get("priority")),
                "status": _text(row.get("status") or "pending"),
            })

    dag = dict(_dict(claim_graph))
    # A claim binding is not relation evidence.  In particular, do not fill a
    # missing DAG basis from the source/target claim's support chunks; the
    # edge must carry its own supplied basis or be downgraded by R3.
    dag["edges"] = [dict(edge) for edge in _edge_rows(dag)]
    dag.setdefault("schema_version", "research_harness.claim_graph.v1")

    topic_identity = _dict(
        blueprint_row.get("topic_identity")
        or blueprint_row.get("review_topic_identity")
    )
    for key in ("topic_id", "topic_key", "topic_slug", "topic_fingerprint"):
        if _text(blueprint_row.get(key)):
            topic_identity.setdefault(key, _text(blueprint_row.get(key)))

    source_artifacts = _dict(phase_run)
    if acceptance:
        source_artifacts["phase3_acceptance"] = _dict(acceptance)
    if output_dir is not None:
        source_artifacts["phase3_output_dir"] = str(Path(output_dir))
    source_artifacts.setdefault("producer", "Phase3ArgumentOrchestrator")
    source_artifacts.setdefault("canonical_filename", R3_HANDOFF_FILENAME)

    return build_r3_production_handoff(
        topic_identity=topic_identity,
        sections=section_rows,
        coverage_atlas=atlas,
        section_argument_contracts=contracts,
        claims_by_criticality=claims_by,
        material_inventory=inventory,
        material_bindings=material_bindings,
        relation_graph=_dict(relation_graph) or {"edges": []},
        claim_dag=dag,
        gaps=gaps,
        coverage_requests=requests,
        synthesis_bundles=bundles,
        visual_bindings=visual_bindings,
        visual_needs=visual_needs,
        source_artifacts=source_artifacts,
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, Mapping) else {}
    except (OSError, ValueError, TypeError):
        return {}


def adapt_phase3_outputs(**kwargs: Any) -> R3ProductionHandoff:
    """Compatibility spelling for the Phase-3 producer adapter."""

    return build_r3_production_handoff_from_phase3(**kwargs)


def build_r3_production_handoff_from_phase3_outputs(**kwargs: Any) -> R3ProductionHandoff:
    """Explicitly named producer-adapter alias for integration callers."""

    return build_r3_production_handoff_from_phase3(**kwargs)


def migrate_r3_production_handoff_payload(raw: Mapping[str, Any]) -> R3ProductionHandoff:
    """Explicitly migrate a known legacy R3 schema spelling to canonical v1.

    ``R3ProductionHandoff.from_dict`` remains a parser only; validation of an
    alias fails closed.  This function is the opt-in compatibility boundary
    and records the source spelling in ``legacy_migration``.
    """

    row = _dict(raw)
    source_version = _text(row.get("schema_version"))
    if source_version == R3_HANDOFF_SCHEMA_VERSION:
        return R3ProductionHandoff.from_dict(row)
    if source_version not in R3_COMPATIBLE_SCHEMA_VERSIONS:
        raise ValueError(
            f"unsupported_r3_schema_for_migration:{source_version or 'missing'}"
        )
    migrated = dict(row)
    migrated["schema_version"] = R3_HANDOFF_SCHEMA_VERSION
    migrated["handoff_version"] = R3_HANDOFF_VERSION
    migrated["handoff_kind"] = R3_HANDOFF_KIND
    marker = _dict(migrated.get("legacy_migration"))
    marker.update({
        "explicit": True,
        "source_schema_version": source_version,
        "target_schema_version": R3_HANDOFF_SCHEMA_VERSION,
        "migration": "r3_handoff_schema_alias_to_canonical_v1",
    })
    migrated["legacy_migration"] = marker
    return R3ProductionHandoff.from_dict(migrated)


def migrate_r3_handoff_schema(raw: Mapping[str, Any]) -> R3ProductionHandoff:
    """Readable alias for the explicit R3 schema migration boundary."""

    return migrate_r3_production_handoff_payload(raw)


def _resolve_path(raw: Any, root: Path) -> Path | None:
    if not raw:
        return None
    candidate = Path(str(raw))
    options = [candidate] if candidate.is_absolute() else [root / candidate, candidate]
    for item in options:
        if item.exists():
            return item.resolve()
    return None


def migrate_legacy_phase3_artifacts(root: Path) -> R3ProductionHandoff:
    """Explicitly migrate the pre-R3 plural-artifact directory.

    This path is intentionally opt-in at the R4 store.  Its inventory is
    conservative and carries a migration marker so callers can distinguish it
    from a producer-created canonical handoff.
    """

    root = Path(root).resolve()
    atlas = _read_json(root / "COVERAGE_ATLAS.json")
    contracts_raw = _read_json(root / "SECTION_ARGUMENT_CONTRACTS.json")
    graph = _read_json(root / "CLAIM_GRAPH.json")
    bindings_raw = _read_json(root / "MATERIAL_BINDINGS.json")
    relation_graph = _read_json(root / "RELATION_GRAPH_MIGRATED.json")
    if not relation_graph:
        relation_graph = _dict(atlas.get("relation_graph")) or {"edges": []}
    requests_raw = _read_json(root / "COVERAGE_REQUESTS.json")
    bundles_raw = _read_json(root / "SYNTHESIS_BUNDLES.json")
    section_ids: list[str] = []
    section_ids.extend(_section_map(atlas.get("sections")).keys())
    section_ids.extend(_normalise_contracts(contracts_raw).keys())
    section_ids.extend(_normalise_bindings(bindings_raw).keys())
    section_ids.extend(_normalise_bundles(bundles_raw).keys())
    claim_rows = _claim_rows(graph.get("claims") or graph.get("nodes"))
    section_ids.extend(_text(item.get("section_id")) for item in claim_rows)
    section_ids = _unique(section_ids)
    sections = [{"section_id": sid} for sid in section_ids]

    inventory: dict[str, dict[str, dict[str, Any]]] = {"papers": {}, "chunks": {}, "visuals": {}}
    ledger_paths: list[Path] = []
    source = _dict(atlas.get("source"))
    ledger_root = _resolve_path(source.get("section_ledgers"), root)
    if ledger_root and ledger_root.is_dir():
        ledger_paths.extend(sorted(ledger_root.glob("*/SECTION_SOURCE_LEDGER.json")))
    ledger_paths.extend(sorted(root.glob("**/SECTION_SOURCE_LEDGER.json")))
    seen_paths: set[str] = set()
    for ledger_path in ledger_paths:
        key = str(ledger_path.resolve()).casefold()
        if key in seen_paths:
            continue
        seen_paths.add(key)
        ledger = _read_json(ledger_path)
        sid = _text(ledger.get("section_id"))
        for row in _list(ledger.get("sources")):
            if not isinstance(row, Mapping):
                continue
            paper_id = _text(row.get("paper_id"))
            if not paper_id:
                continue
            _merge_inventory_record(inventory, "papers", paper_id, row, sid)
            for chunk_id in _unique(row.get("canonical_chunk_ids")):
                _merge_inventory_record(inventory, "chunks", chunk_id, {
                    "chunk_id": chunk_id,
                    "paper_id": paper_id,
                    "use_permission": row.get("use_permission", DISCOVERY),
                    "scope_fit": row.get("scope_fit", "unreviewed"),
                    "content_depth": row.get("content_depth", "metadata"),
                    "context_complete": row.get("context_complete", False),
                    "source_kind": row.get("source_kind", "metadata"),
                }, sid)

    binding_sections = _normalise_bindings(bindings_raw)
    for sid, binding_section in binding_sections.items():
        for claim in _claim_rows(binding_section.get("claims")):
            binding = dict(claim)
            permission = _lower(binding.get("permission_status"))
            default_permission = FACTUAL if permission == "bound" else QUALIFIED if permission == "qualified_only" else DISCOVERY
            for chunk_id in _binding_ids(binding)["supporting_chunks"] + _binding_ids(binding)["candidate_chunks"]:
                if chunk_id not in inventory["chunks"]:
                    _merge_inventory_record(inventory, "chunks", chunk_id, {
                        "chunk_id": chunk_id,
                        "use_permission": default_permission,
                        "scope_fit": "unreviewed",
                        "content_depth": "fulltext" if default_permission == FACTUAL else "metadata",
                        "context_complete": default_permission == FACTUAL,
                        "source_kind": "legacy_migration",
                    }, sid)
            for paper_id in _binding_ids(binding)["papers"]:
                if paper_id not in inventory["papers"]:
                    _merge_inventory_record(inventory, "papers", paper_id, {
                        "paper_id": paper_id,
                        "use_permission": default_permission,
                        "scope_fit": "unreviewed",
                        "content_depth": "fulltext" if default_permission == FACTUAL else "metadata",
                        "context_complete": default_permission == FACTUAL,
                    }, sid)

    visual_ids: set[str] = set()
    for bundle in _normalise_bundles(bundles_raw).values():
        visual_ids.update(_unique(bundle.get("visual_chunk_ids") or bundle.get("visual_asset_ids")))
    for visual_id in sorted(visual_ids):
        inventory["visuals"][visual_id] = {
            "visual_id": visual_id,
            "status": "unknown",
            "migration_inferred": True,
        }

    claims_by: dict[str, list[dict[str, Any]]] = {key: [] for key in R3_CRITICALITIES}
    for claim in claim_rows:
        row = dict(claim)
        row.setdefault("section_id", _text(row.get("section_id")))
        claims_by.setdefault(_normalise_criticality(row), []).append(row)
    contracts = _normalise_contracts(contracts_raw)
    for sid in section_ids:
        contracts.setdefault(sid, {"section_id": sid, "status": "legacy_migrated"})
    bundles = _normalise_bundles(bundles_raw)
    for sid in section_ids:
        bundles.setdefault(sid, {"section_id": sid, "status": "legacy_migrated"})
    bindings = _normalise_bindings(bindings_raw)
    for sid in section_ids:
        bindings.setdefault(sid, {"section_id": sid, "claims": {}})
    visual_bindings = {
        sid: [
            {"visual_binding_id": f"{sid}:legacy_visual:{index:02d}", "section_id": sid, "visual_id": visual_id, "status": "legacy_migrated"}
            for index, visual_id in enumerate(
                _unique(bundles.get(sid, {}).get("visual_chunk_ids") or bundles.get(sid, {}).get("visual_asset_ids")),
                start=1,
            )
        ]
        for sid in section_ids
    }
    visual_needs = {sid: [] for sid in section_ids}
    requests = requests_raw.get("requests") or []
    gaps = []
    for request in _list(requests):
        if not isinstance(request, Mapping):
            continue
        row = dict(request)
        gaps.append({
            "gap_id": _text(row.get("request_id")) or f"legacy_gap:{_text(row.get('section_id'))}:{len(gaps) + 1:02d}",
            "section_id": _text(row.get("section_id")),
            "kind": "legacy_coverage_request",
            "claim_ids": _unique(row.get("missing_claim_ids")),
            "blocking": _lower(row.get("priority")) == "load_bearing",
        })

    return build_r3_production_handoff(
        topic_identity=_dict(atlas.get("topic_identity")),
        sections=sections,
        coverage_atlas=atlas,
        section_argument_contracts=contracts,
        claims_by_criticality=claims_by,
        material_inventory=inventory,
        material_bindings=bindings,
        relation_graph=relation_graph,
        claim_dag=graph,
        gaps=gaps,
        coverage_requests=requests,
        synthesis_bundles=bundles,
        visual_bindings=visual_bindings,
        visual_needs=visual_needs,
        source_artifacts={"legacy_root": str(root)},
        legacy_migration={
            "source": "plural_phase3_artifacts",
            "source_root": str(root),
            "explicit": True,
            "warning": "Legacy IDs and permissions were migrated conservatively; rerun Phase 3 for a producer handoff.",
        },
    )


def write_r3_production_handoff(
    path: Path,
    handoff: R3ProductionHandoff,
    *,
    fail_on_invalid: bool = False,
) -> R3ValidationReport:
    """Validate, refresh readiness, and write one canonical JSON handoff."""

    report = handoff.validate()
    handoff.readiness = {
        "sections": report.section_readiness,
        "global": report.global_readiness,
        "validation": report.to_dict(),
    }
    if fail_on_invalid and not report.valid:
        raise R3HandoffValidationError(report)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(handoff.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(target)
    return report


def read_r3_production_handoff(
    path: Path,
    *,
    fail_on_invalid: bool = False,
) -> tuple[R3ProductionHandoff, R3ValidationReport]:
    handoff = R3ProductionHandoff.from_dict(_read_json(Path(path)))
    report = handoff.validate()
    if fail_on_invalid and not report.valid:
        raise R3HandoffValidationError(report)
    handoff.readiness = {
        "sections": report.section_readiness,
        "global": report.global_readiness,
        "validation": report.to_dict(),
    }
    return handoff, report


# Short public aliases keep the schema vocabulary readable for callers while
# retaining the R3-prefixed class names used by the implementation.
SectionArgumentContract = R3SectionArgumentContract
MaterialBinding = R3MaterialBinding
SynthesisBundle = R3CompactSynthesisBundle
ProductionHandoff = R3ProductionHandoff


__all__ = [
    "CanonicalIdentityResolver",
    "CoverageAtlas",
    "MaterialBinding",
    "ProductionHandoff",
    "R3Claim",
    "R3_CLAIM_CLASSIFICATIONS",
    "R3CompactSynthesisBundle",
    "R3HandoffValidationError",
    "R3_HANDOFF_FILENAME",
    "R3_HANDOFF_SCHEMA_VERSION",
    "R3_HANDOFF_VERSION",
    "R3_SECTION_OUTCOMES",
    "R3MaterialBinding",
    "R3ProductionHandoff",
    "R3SectionArgumentContract",
    "R3ValidationReport",
    "SectionArgumentContract",
    "SynthesisBundle",
    "adapt_phase3_outputs",
    "build_canonical_identity_resolver",
    "build_r3_production_handoff",
    "build_r3_production_handoff_from_phase3",
    "build_r3_production_handoff_from_phase3_outputs",
    "migrate_legacy_phase3_artifacts",
    "migrate_r3_handoff_schema",
    "migrate_r3_production_handoff_payload",
    "read_r3_production_handoff",
    "validate_r3_production_handoff",
    "write_r3_production_handoff",
]
