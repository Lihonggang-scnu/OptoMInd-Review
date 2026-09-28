"""Reusable guards for the bounded Phase-2/Phase-3 coverage feedback loop.

The loop itself is deliberately orchestrated by the acceptance runner so a
single section can be audited without silently becoming an eight-section
production run.  This module contains the deterministic parts that must be
shared by any future production caller: wave budgets and the mapping from a
retrieval query to the scientific component it is meant to recover.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class BoundedWaveBudget:
    """A hard materialization budget shared by all waves in one section."""

    per_wave: int = 3
    total: int = 5
    max_waves: int = 2

    def budget_for_wave(self, wave_index: int, already_materialized: int) -> int:
        """Return the allowed number for a 1-based wave, never negative."""

        if wave_index < 1 or wave_index > self.max_waves:
            return 0
        remaining = max(0, int(self.total) - int(already_materialized))
        return min(max(0, int(self.per_wave)), remaining)

    def valid(self) -> bool:
        return (
            self.per_wave >= 0
            and self.total >= 0
            and self.max_waves >= 0
            and self.per_wave <= self.total
        )


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def canonical_material_identity(record: dict[str, Any]) -> str:
    """Return the stable paper identity used across waves and routes.

    Candidate IDs are intentionally excluded: discovery can emit a new
    candidate ID for the same DOI (or CorpusId) on every search.  DOI is the
    strongest identity, followed by CorpusId, paper_id, and finally a
    normalized title for DOI-less records.
    """

    doi = _clean(record.get("doi")).casefold()
    if doi.startswith("doi:"):
        doi = doi[4:]
    if doi:
        return f"doi:{doi}"
    corpus_id = _clean(
        record.get("corpus_id")
        or record.get("semantic_scholar_id")
        or record.get("semantic_scholar_paper_id")
    ).casefold()
    if corpus_id:
        return f"s2:{corpus_id}"
    paper_id = _clean(record.get("paper_id")).casefold()
    if paper_id:
        return f"paper:{paper_id}"
    title = _clean(record.get("title")).casefold()
    return f"title:{title}" if title else ""


def route_record_identity(record: dict[str, Any]) -> tuple[str, ...]:
    """Build the deterministic identity for one route-audit record.

    Chunk records include ``chunk_id``; paper records do not.  The explicit
    type marker prevents a paper-level receipt from collapsing into its first
    chunk while still collapsing the same chunk emitted by two waves.
    """

    # Candidate IDs are discovery receipts, not material identity.  A later
    # search can assign a new ID to the same DOI/CorpusId; retaining that ID
    # in the key would reintroduce duplicate paper/chunk rows across waves.
    return (
        "chunk" if _clean(record.get("chunk_id")) else "paper",
        canonical_material_identity(record),
        _clean(record.get("chunk_id")),
        _clean(record.get("discovery_route")),
        _clean(record.get("materialization_route")),
        _clean(record.get("acquisition_status")),
    )


def _merge_list_values(left: Any, right: Any) -> list[Any]:
    values: list[Any] = []
    for raw in (left, right):
        if isinstance(raw, list):
            values.extend(raw)
        elif raw not in (None, "", {}):
            values.append(raw)
    return list(dict.fromkeys(value for value in values if value not in (None, "")))


def deduplicate_route_records(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Collapse repeated wave receipts without dropping chunk provenance."""

    merged: dict[tuple[str, ...], dict[str, Any]] = {}
    order: list[tuple[str, ...]] = []
    input_count = 0
    for raw in records:
        if not isinstance(raw, dict):
            continue
        input_count += 1
        row = dict(raw)
        key = route_record_identity(row)
        if key not in merged:
            row["waves"] = _merge_list_values(row.get("waves"), row.get("wave"))
            row["candidate_ids"] = _merge_list_values(
                row.get("candidate_ids"), row.get("candidate_id")
            )
            if row.get("chunk_id"):
                row["chunk_ids"] = _merge_list_values(
                    row.get("chunk_ids"), row.get("chunk_id")
                )
            merged[key] = row
            order.append(key)
            continue
        target = merged[key]
        target["waves"] = _merge_list_values(target.get("waves"), row.get("wave"))
        target["candidate_ids"] = _merge_list_values(
            target.get("candidate_ids"), row.get("candidate_ids")
        )
        target["candidate_ids"] = _merge_list_values(
            target.get("candidate_ids"), row.get("candidate_id")
        )
        target["query"] = _merge_list_values(target.get("query"), row.get("query"))
        target["missing_components"] = _merge_list_values(
            target.get("missing_components"), row.get("missing_components")
        )
        target["chunk_ids"] = _merge_list_values(
            target.get("chunk_ids"), row.get("chunk_ids"),
        )
        # A duplicate receipt must never turn an actually-new row into a
        # synthetic success.  Count inserted chunks from the durable fields.
        target["new_chunks"] = max(
            int(target.get("new_chunks") or 0), int(row.get("new_chunks") or 0)
        )
        target["new_paper"] = bool(
            target.get("new_paper")
            and int(target.get("new_chunks") or 0) > 0
        )
    rows = [merged[key] for key in order]
    for row in rows:
        row["new_paper"] = bool(
            row.get("new_paper") and int(row.get("new_chunks") or 0) > 0
        )
    return {
        "records": rows,
        "input_count": input_count,
        "unique_count": len(rows),
        "duplicates_removed": max(0, input_count - len(rows)),
        "identity_fields": [
            "record_type", "paper_identity", "chunk_id",
            "discovery_route", "materialization_route", "acquisition_status",
        ],
        "provenance_merged_fields": ["candidate_id", "candidate_ids", "waves"],
    }


def deduplicate_material_records(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Merge per-wave paper receipts into a truthful new-material view."""

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    input_count = 0
    for raw in records:
        if not isinstance(raw, dict):
            continue
        input_count += 1
        row = dict(raw)
        key = (canonical_material_identity(row), _clean(row.get("candidate_id")))
        # If discovery created a second candidate ID for the same paper, DOI
        # identity still wins over candidate identity.
        if key[0]:
            key = (key[0], "")
        if key not in merged:
            row["waves"] = _merge_list_values(row.get("waves"), row.get("wave"))
            row["chunk_ids"] = _merge_list_values(row.get("chunk_ids"), None)
            row["new_chunk_ids"] = _merge_list_values(row.get("new_chunk_ids"), None)
            merged[key] = row
            order.append(key)
            continue
        target = merged[key]
        target["waves"] = _merge_list_values(target.get("waves"), row.get("wave"))
        target["chunk_ids"] = _merge_list_values(
            target.get("chunk_ids"), row.get("chunk_ids")
        )
        target["new_chunk_ids"] = _merge_list_values(
            target.get("new_chunk_ids"), row.get("new_chunk_ids")
        )
        target["new_chunks"] = len(target.get("new_chunk_ids") or []) or max(
            int(target.get("new_chunks") or 0), int(row.get("new_chunks") or 0)
        )
        target["reused_chunks"] = max(
            int(target.get("reused_chunks") or 0), int(row.get("reused_chunks") or 0)
        )
        target["new_paper"] = bool(
            (target.get("new_paper") or row.get("new_paper"))
            and int(target.get("new_chunks") or 0) > 0
        )
        target["paper_row_inserted"] = bool(
            target.get("paper_row_inserted") or row.get("paper_row_inserted")
        )
    rows = [merged[key] for key in order]
    for row in rows:
        if row.get("new_chunk_ids"):
            row["new_chunks"] = len(row["new_chunk_ids"])
        row["new_paper"] = bool(
            row.get("new_paper") and int(row.get("new_chunks") or 0) > 0
        )
    return {
        "records": rows,
        "input_count": input_count,
        "unique_count": len(rows),
        "duplicates_removed": max(0, input_count - len(rows)),
    }


def target_components(request: dict[str, Any]) -> list[str]:
    """Return explicit scientific components in stable request order."""

    result: list[str] = []
    for target in request.get("query_targets") or []:
        if not isinstance(target, dict):
            continue
        for value in target.get("missing_components") or []:
            cleaned = _clean(value)
            if cleaned:
                result.append(cleaned)
    return list(dict.fromkeys(result))


def select_next_wave_request(
    request: dict[str, Any],
    *,
    blocked_components: Iterable[str] = (),
) -> dict[str, Any]:
    """Keep the next bounded wave on an unresolved, untried component."""

    blocked = {_clean(item).casefold() for item in blocked_components if _clean(item)}
    result = dict(request)
    targets = [
        dict(item) for item in request.get("query_targets") or []
        if isinstance(item, dict) and _clean(item.get("query"))
    ]
    if not targets or not blocked:
        result["wave_target_components"] = target_components(result)
        return result
    eligible = [
        target for target in targets
        if not any(
            _clean(component).casefold() in blocked
            for component in target.get("missing_components") or []
        )
    ]
    result["query_targets"] = eligible
    result["queries"] = [_clean(item.get("query")) for item in eligible]
    result["wave_target_components"] = target_components(result)
    result["no_progress_components_excluded"] = sorted(blocked)
    if not eligible:
        result["stop_reason"] = "bounded_novel_components_exhausted"
    return result


def mark_no_progress_candidates(
    state: dict[str, Any],
    candidate_records: Iterable[dict[str, Any]],
    components: Iterable[str],
    *,
    wave: int,
) -> dict[str, Any]:
    """Mark materialized candidates whose target components stayed open.

    This helper only records an observed lack of coverage improvement.  It
    never closes a component and never changes evidence permissions.
    """

    values = list(dict.fromkeys(
        _clean(item) for item in components if _clean(item)
    ))
    outcomes = state.setdefault("candidate_outcomes", {})
    identity_index = state.setdefault("material_identity_index", {})
    state.setdefault("no_progress_escalations", [])
    for row in candidate_records:
        if not isinstance(row, dict):
            continue
        candidate_id = _clean(row.get("candidate_id"))
        identity = canonical_material_identity(row)
        if not candidate_id and not identity:
            continue
        key = candidate_id or identity
        outcome = outcomes.setdefault(
            key,
            {
                "candidate_id": candidate_id,
                "material_identity": identity,
                "attempted_waves": [],
                "materialization_attempts": 0,
                "new_chunk_ids": [],
                "reused_chunk_ids": [],
                "last_materialization_status": "",
                "no_progress": False,
                "no_progress_components": [],
            },
        )
        if identity:
            ids = identity_index.setdefault(identity, [])
            if candidate_id and candidate_id not in ids:
                ids.append(candidate_id)
        if wave and wave not in outcome.setdefault("attempted_waves", []):
            outcome["attempted_waves"].append(wave)
        outcome["no_progress"] = True
        outcome["no_progress_components"] = list(dict.fromkeys([
            *(outcome.get("no_progress_components") or []),
            *values,
        ]))
        state["no_progress_escalations"].append({
            "candidate_id": candidate_id,
            "material_identity": identity,
            "wave": wave,
            "components": values,
        })
    state["no_progress_escalations"] = state["no_progress_escalations"][-100:]
    return state


def _claim_components(claim: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("missing_evidence_components", "missing_components"):
        raw = claim.get(key) or []
        if isinstance(raw, str):
            raw = [raw]
        if isinstance(raw, list):
            values.extend(_clean(item) for item in raw if _clean(item))
    return list(dict.fromkeys(values))


def build_query_component_map(
    request: dict[str, Any],
    claims: Iterable[dict[str, Any]] = (),
) -> dict[str, list[str]]:
    """Associate each clustered query with the gap components it targets.

    This is intentionally transparent and conservative.  It does not claim
    that a query proves a component; it only records the retrieval intent so
    every later paper/chunk can be audited against the request that caused it.
    """

    explicit_targets = [
        dict(item) for item in request.get("query_targets") or []
        if isinstance(item, dict) and _clean(item.get("query"))
    ]
    query_list = [
        _clean(item.get("query") if isinstance(item, dict) else item)
        for item in request.get("queries") or []
        if _clean(item.get("query") if isinstance(item, dict) else item)
    ]
    if explicit_targets:
        query_list = [_clean(item.get("query")) for item in explicit_targets]
    claim_map = {
        str(item.get("claim_id")): _claim_components(item)
        for item in claims
        if isinstance(item, dict) and item.get("claim_id")
    }
    missing_by_claim = {
        str(item): list(claim_map.get(str(item), []))
        for item in request.get("missing_claim_ids") or []
    }
    all_components = [
        component
        for claim_id in request.get("missing_claim_ids") or []
        for component in missing_by_claim.get(str(claim_id), [])
    ]
    all_components = list(dict.fromkeys(all_components))

    # Prefer the explicit generation-time mapping.  It is the only production
    # path: later code must not infer scientific ownership from substring
    # overlap in a query.
    if explicit_targets:
        result: dict[str, list[str]] = {}
        for target in explicit_targets:
            query = _clean(target.get("query"))
            values = [
                _clean(value) for value in target.get("missing_components") or []
                if _clean(value)
            ]
            if not values:
                for claim_id in target.get("claim_ids") or []:
                    values.extend(claim_map.get(str(claim_id), []))
            result[query] = list(dict.fromkeys(values))
        return result

    # Compatibility fallback for pre-Phase-2.1 requests.  It is retained so
    # old artifacts can be replayed, but new CoverageRequest objects always
    # carry query_targets and never use this heuristic.
    groups = (
        ({"hermitian", "diabolical", "orthogonal", "eigenvector", "eigenvalue"},
         ("hermitian", "diabolical", "orthogonal", "eigenvector", "eigenvalue")),
        ({"jordan", "block", "canonical", "chain", "defective"},
         ("jordan", "block", "canonical", "chain", "defective")),
        ({"algebraic", "geometric", "multiplicity", "generalized"},
         ("algebraic", "geometric", "multiplicity", "generalized")),
        ({"branch", "resolvent", "riemann", "parameter", "complex"},
         ("branch", "resolvent", "riemann", "parameter", "complex")),
    )
    result: dict[str, list[str]] = {}
    for query in query_list:
        lowered = query.casefold()
        query_groups = [tokens for query_tokens, tokens in groups if query_tokens & set(lowered.replace("-", " ").split())]
        matched = []
        for component in all_components:
            component_lower = component.casefold()
            if any(any(token in component_lower for token in tokens) for tokens in query_groups):
                matched.append(component)
        if not matched and all_components:
            # Round-robin assignment preserves intent even when a query uses
            # vocabulary different from the claim's missing-component text.
            matched = [all_components[len(result) % len(all_components)]]
        result[query] = list(dict.fromkeys(matched))
    return result


def compact_route_provenance(record: dict[str, Any]) -> dict[str, Any]:
    """Keep the route audit useful without copying full candidate payloads."""

    keys = (
        "wave", "paper_id", "chunk_id", "doi", "corpus_id", "material_identity",
        "discovery_route",
        "materialization_route", "content_depth", "use_permission",
        "scope_fit", "source_kind", "retrieval_role", "retrieval_query",
        "query", "missing_components", "candidate_id", "candidate_ids",
        "acquisition_status", "all_chunk_ids", "new_chunk_ids", "new_paper",
        "paper_row_inserted", "new_chunks", "reused_chunks",
    )
    return {
        key: record.get(key)
        for key in keys
        if record.get(key) not in (None, "", [], {})
    }


def ensure_explicit_query_targets(
    request: dict[str, Any],
    claims: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Attach generation-time query ownership to a legacy request once.

    This adapter is for replaying pre-2.1 artifacts.  New producers should
    populate ``query_targets`` directly in ``CoverageRequest``.  The adapter
    uses the request's claim/component order, never lexical similarity.
    """

    result = dict(request)
    if result.get("query_targets"):
        return result
    pairs: list[dict[str, Any]] = []
    by_id = {
        str(item.get("claim_id")): item
        for item in claims
        if isinstance(item, dict) and item.get("claim_id")
    }
    for claim_id in result.get("missing_claim_ids") or []:
        claim = by_id.get(str(claim_id), {})
        components = _claim_components(claim)
        for index, component in enumerate(components):
            pairs.append({
                "claim_id": str(claim_id),
                "missing_component_id": f"{claim_id}::component_{index + 1}",
                "missing_component": component,
            })
    targets = []
    for index, raw_query in enumerate(result.get("queries") or []):
        query = _clean(raw_query.get("query") if isinstance(raw_query, dict) else raw_query)
        if not query:
            continue
        selected = [pairs[index % len(pairs)]] if pairs else []
        targets.append({
            "query": query,
            "claim_ids": [item["claim_id"] for item in selected],
            "missing_component_ids": [item["missing_component_id"] for item in selected],
            "missing_components": [item["missing_component"] for item in selected],
            "generated_by": "legacy_replay_adapter",
        })
    result["query_targets"] = targets
    return result
