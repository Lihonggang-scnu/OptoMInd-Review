"""Domain-agnostic fresh-evidence retrieval and bounded semantic judging."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from typing import Any, Callable, Iterable

from .argument_quality_policy import DISCOVERY, evidence_ceiling


SUPPORT_STATES = {"supported", "partially_supported", "unsupported"}

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "into", "is", "it", "of", "on", "or", "that", "the", "their",
    "this", "to", "was", "were", "which", "with",
}
_NEGATIONS = {"no", "not", "never", "neither", "nor", "without"}
_QUANTITATIVE_COMPARISONS = {
    "at least", "at most", "equal to", "fewer than", "greater than",
    "less than", "more than", "no less than", "no more than",
}

_PROPOSITION_RESIDUAL_PREFIX = "Unverified proposition coverage:"


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _unique(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def normalize_support_state(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized in {"supported", "closed", "bound", "covered"}:
        return "supported"
    if normalized in {
        "partially_supported", "partial", "qualified", "qualified_only",
    }:
        return "partially_supported"
    return "unsupported"


def normalize_scientific_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = (
        text.replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2212", "-")
    )
    return " ".join(text.split()).casefold()


def normalize_residual(value: Any) -> str:
    """Collapse recursive generic wrappers while preserving specific gaps."""

    residual = _clean(value)
    prefix_key = _PROPOSITION_RESIDUAL_PREFIX.casefold()
    while residual.casefold().startswith(prefix_key):
        inner = residual[len(_PROPOSITION_RESIDUAL_PREFIX):].strip()
        if not inner:
            return _PROPOSITION_RESIDUAL_PREFIX
        if inner.casefold().startswith(prefix_key):
            residual = inner
            continue
        if inner.casefold().startswith("unverified "):
            return inner
        return f"{_PROPOSITION_RESIDUAL_PREFIX} {inner}"
    return residual


def normalize_residuals(values: Iterable[Any]) -> list[str]:
    return _unique(
        normalized
        for value in values
        if (normalized := normalize_residual(value))
    )


def proposition_coverage_residual(value: Any) -> str:
    residual = normalize_residual(value)
    if not residual or residual.casefold().startswith("unverified "):
        return residual
    return f"{_PROPOSITION_RESIDUAL_PREFIX} {residual}"


def _token_key(token: str) -> str:
    value = normalize_scientific_text(token).strip("-_/.")
    if len(value) > 5 and value.endswith("ies"):
        return value[:-3] + "y"
    for suffix in ("ing", "ed", "es", "s"):
        if len(value) > len(suffix) + 3 and value.endswith(suffix):
            return value[:-len(suffix)]
    return value


def scientific_tokens(value: Any) -> list[str]:
    normalized = normalize_scientific_text(value).replace("-", " ")
    raw = re.findall(
        r">=|<=|!=|==|[><=]|[\u2265\u2264\u2260\u2248\u221d]|"
        r"\d+(?:\.\d+)?%?|[^\W_]+",
        normalized,
        flags=re.UNICODE,
    )
    return [
        key
        for item in raw
        if (key := _token_key(item)) and key not in _STOPWORDS
    ]


def scientific_phrases(value: Any) -> set[str]:
    tokens = scientific_tokens(value)
    return {
        " ".join(tokens[index:index + width])
        for width in (2, 3)
        for index in range(max(0, len(tokens) - width + 1))
    }


def extract_precision_constraints(requested: str) -> list[dict[str, str]]:
    """Extract generic precision that cannot be silently weakened."""

    raw = unicodedata.normalize("NFKC", str(requested or ""))
    normalized = normalize_scientific_text(raw)
    constraints: list[dict[str, str]] = []

    def add(kind: str, value: Any) -> None:
        cleaned = _clean(value).strip(" ,.;:")
        key = (kind, normalize_scientific_text(cleaned))
        existing = {
            (item["kind"], normalize_scientific_text(item["value"]))
            for item in constraints
        }
        if cleaned and key not in existing:
            constraints.append({"kind": kind, "value": cleaned})

    for match in re.finditer(
        r"(?<![\w.])\d+(?:\.\d+)?\s*(?:%|[A-Za-z]{1,5}(?:[/^-][A-Za-z0-9]{1,5})*)?",
        raw,
    ):
        add("numeric", match.group(0))
    for match in re.finditer(
        r"\b(?:order|size|rank|degree|dimension)\s*(?:of|=|:)?\s*"
        r"(?:[A-Za-z]|\d+)\b|\b(?:n|\d+)(?:st|nd|rd|th)-?order\b",
        normalized,
    ):
        add("order_or_size", match.group(0))
    for marker in sorted(_QUANTITATIVE_COMPARISONS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(marker)}\b", normalized):
            add("comparison", marker)
    for symbol in re.findall(
        r">=|<=|!=|==|[><=]|[\u2265\u2264\u2260]",
        raw,
    ):
        add("comparison", symbol)
    for marker in sorted(_NEGATIONS):
        if re.search(rf"\b{re.escape(marker)}\b", normalized):
            add("negation", marker)
    for entity in re.findall(r"\b[A-Z][A-Z0-9-]{1,}\b", raw):
        add("named_entity", entity)
    for entity in re.findall(r"[\"'`](.*?)[\"'`]", raw):
        add("named_entity", entity)
    return constraints


def _constraint_present(constraint: dict[str, str], candidate_text: str) -> bool:
    value = normalize_scientific_text(constraint.get("value"))
    candidate = normalize_scientific_text(candidate_text)
    if constraint.get("kind") == "numeric":
        return re.sub(r"\s+", "", value) in re.sub(r"\s+", "", candidate)
    value_tokens = set(scientific_tokens(value))
    candidate_tokens = set(scientific_tokens(candidate))
    return bool(value_tokens) and value_tokens <= candidate_tokens


def missing_precision_constraints(
    constraints: Iterable[dict[str, str]],
    candidate_text: str,
) -> list[dict[str, str]]:
    return [
        dict(item) for item in constraints
        if not _constraint_present(item, candidate_text)
    ]


def precision_residuals(constraints: Iterable[dict[str, str]]) -> list[str]:
    labels = {
        "numeric": "numeric constraint",
        "order_or_size": "order/size qualifier",
        "comparison": "comparison/inequality",
        "negation": "negation",
        "named_entity": "named technical entity",
    }
    return normalize_residuals(
        f"Unverified {labels.get(str(item.get('kind')), 'precision')}: {item.get('value')}"
        for item in constraints
        if item.get("value")
    )


def _precision_conflict_count(
    constraints: Iterable[dict[str, str]],
    candidate_text: str,
) -> int:
    candidate_constraints = extract_precision_constraints(candidate_text)
    count = 0
    for item in constraints:
        if _constraint_present(item, candidate_text):
            continue
        kind = item.get("kind")
        alternatives = [
            row for row in candidate_constraints if row.get("kind") == kind
        ]
        if kind in {"numeric", "named_entity", "comparison"} and alternatives:
            count += 1
        if kind == "negation" and item.get("value", "").casefold() == "without":
            if re.search(r"\bwith\b", normalize_scientific_text(candidate_text)):
                count += 1
    return count


def eligible_fresh_sentences(
    records_by_id: dict[str, dict[str, Any]],
    fresh_chunk_ids: Iterable[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for chunk_id in sorted(_unique(fresh_chunk_ids)):
        record = records_by_id.get(chunk_id)
        text = str((record or {}).get("text") or (record or {}).get("normalized_text") or "").strip()
        if not record or not text or evidence_ceiling(record)[0] == DISCOVERY:
            continue
        depth = str(record.get("content_depth") or "").casefold()
        if depth not in {
            "fulltext", "structured_snippet", "s2_snippet", "s2_body",
            "publisher_html", "pdf", "html_markdown",
        }:
            continue
        if not bool(record.get("context_complete")) and depth in {
            "structured_snippet", "s2_snippet", "s2_body",
        }:
            continue
        sentences = [
            _clean(item)
            for item in re.split(r"(?<=[.!?])\s+|[\r\n]+", text)
            if _clean(item)
        ] or [_clean(text)]
        for index, sentence in enumerate(sentences):
            tokens = scientific_tokens(sentence)
            if not tokens:
                continue
            rows.append({
                "candidate_id": f"{chunk_id}::sentence::{index + 1}",
                "chunk_id": chunk_id,
                "paper_id": str(record.get("paper_id") or ""),
                "excerpt": sentence[:700],
                "tokens": tokens,
                "phrases": scientific_phrases(sentence),
                "permission_ceiling": evidence_ceiling(record)[0],
                "content_depth": record.get("content_depth", ""),
            })
    return rows


def rank_fresh_candidates(
    requested: str,
    sentence_rows: Iterable[dict[str, Any]],
    token_document_frequency: dict[str, int],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    query_set = set(scientific_tokens(requested))
    query_phrases = scientific_phrases(requested)
    sentence_rows = list(sentence_rows)
    if not query_set or not sentence_rows:
        return []
    total_documents = len(sentence_rows)
    weights = {
        token: 1.0 + math.log(
            (1.0 + total_documents)
            / (1.0 + token_document_frequency.get(token, 0))
        )
        for token in query_set
    }
    total_weight = sum(weights.values()) or 1.0
    ranked: list[dict[str, Any]] = []
    for row in sentence_rows:
        candidate_set = set(row.get("tokens") or [])
        overlap = query_set & candidate_set
        if not overlap:
            continue
        weighted_coverage = sum(weights[token] for token in overlap) / total_weight
        jaccard = len(overlap) / max(1, len(query_set | candidate_set))
        phrase_overlap = query_phrases & set(row.get("phrases") or set())
        phrase_coverage = len(phrase_overlap) / max(1, len(query_phrases))
        score = 0.65 * weighted_coverage + 0.20 * jaccard + 0.15 * phrase_coverage
        if score < 0.04:
            continue
        ranked.append({
            "candidate_id": row["candidate_id"],
            "chunk_id": row["chunk_id"],
            "paper_id": row.get("paper_id", ""),
            "excerpt": row["excerpt"],
            "score": round(score, 6),
            "weighted_token_coverage": round(weighted_coverage, 6),
            "matched_tokens": sorted(overlap),
            "matched_phrases": sorted(phrase_overlap),
            "permission_ceiling": row.get("permission_ceiling", ""),
            "content_depth": row.get("content_depth", ""),
        })
    ranked.sort(key=lambda item: (
        -float(item["score"]),
        str(item["chunk_id"]),
        str(item["candidate_id"]),
    ))
    unique_chunks: list[dict[str, Any]] = []
    seen_chunks: set[str] = set()
    for item in ranked:
        if item["chunk_id"] in seen_chunks:
            continue
        seen_chunks.add(item["chunk_id"])
        unique_chunks.append(item)
        if len(unique_chunks) >= limit:
            break
    return unique_chunks


def _unsupported_audit(
    component_id: str,
    claim_id: str,
    requested: str,
    constraints: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "component_id": component_id,
        "claim_id": claim_id,
        "requested_component": requested,
        "status": "unsupported",
        "support_state": "unsupported",
        "supported_component": "",
        "chunk_ids": [],
        "paper_ids": [],
        "evidence_spans": [],
        "precision_constraints": constraints,
        "residual_components": normalize_residuals(
            precision_residuals(constraints) or [requested]
        ),
        "ranked_candidates": [],
        "decision_source": "deterministic_scientific_token_ranking",
    }


def audit_fresh_components(
    claims: Iterable[dict[str, Any]],
    records_by_id: dict[str, dict[str, Any]],
    fresh_chunk_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """Rank eligible sentences for each unresolved component without domain rules."""

    sentence_rows = eligible_fresh_sentences(records_by_id, fresh_chunk_ids)
    document_frequency: dict[str, int] = {}
    for sentence in sentence_rows:
        for token in set(sentence["tokens"]):
            document_frequency[token] = document_frequency.get(token, 0) + 1
    audits: list[dict[str, Any]] = []
    for claim in claims:
        claim_id = _clean(claim.get("claim_id"))[:120]
        missing = _unique(
            _clean(item)
            for item in (
                claim.get("missing_evidence_components")
                or claim.get("missing_components")
                or []
            )
            if _clean(item)
        )
        for requested in missing:
            component_id = (
                f"{claim_id}::fresh_component::"
                f"{hashlib.sha1(requested.encode('utf-8')).hexdigest()[:12]}"
            )
            constraints = extract_precision_constraints(requested)
            ranked = rank_fresh_candidates(
                requested,
                sentence_rows,
                document_frequency,
            )
            if not ranked:
                audits.append(_unsupported_audit(
                    component_id, claim_id, requested, constraints
                ))
                continue
            top = ranked[0]
            missing_constraints = missing_precision_constraints(
                constraints,
                top["excerpt"],
            )
            conflict_count = _precision_conflict_count(
                constraints,
                top["excerpt"],
            )
            coverage = float(top.get("weighted_token_coverage") or 0.0)
            has_phrase_match = bool(top.get("matched_phrases"))
            if conflict_count >= 2 or (coverage < 0.18 and not has_phrase_match):
                state = "unsupported"
            elif not missing_constraints and coverage >= 0.72:
                state = "supported"
            else:
                state = "partially_supported"
            residual = precision_residuals(missing_constraints)
            if state == "partially_supported" and not residual:
                residual = [proposition_coverage_residual(requested)]
            if state == "unsupported":
                residual = normalize_residuals(residual or [requested])
            cited = [top] if state != "unsupported" else []
            audits.append({
                "component_id": component_id,
                "claim_id": claim_id,
                "requested_component": requested,
                "status": state,
                "support_state": state,
                "supported_component": top["excerpt"] if cited else "",
                "chunk_ids": [top["chunk_id"]] if cited else [],
                "paper_ids": _unique(
                    item.get("paper_id") for item in cited if item.get("paper_id")
                ),
                "evidence_spans": [
                    {
                        "chunk_id": item["chunk_id"],
                        "quote": item["excerpt"],
                        "permission_ceiling": item["permission_ceiling"],
                        "content_depth": item["content_depth"],
                    }
                    for item in cited
                ],
                "precision_constraints": constraints,
                "residual_components": residual,
                "ranked_candidates": ranked,
                "decision_source": "deterministic_scientific_token_ranking",
            })
    return audits


def _proposition_traceable(
    proposition: str,
    cited_candidates: Iterable[dict[str, Any]],
) -> bool:
    excerpts = " ".join(
        _clean(item.get("excerpt"))
        for item in cited_candidates
        if _clean(item.get("excerpt"))
    )
    if not proposition or not excerpts:
        return False
    normalized_proposition = normalize_scientific_text(proposition)
    normalized_excerpts = normalize_scientific_text(excerpts)
    if normalized_proposition in normalized_excerpts:
        return True
    proposition_tokens = set(scientific_tokens(proposition))
    excerpt_tokens = set(scientific_tokens(excerpts))
    if not proposition_tokens:
        return False
    invented_precision = missing_precision_constraints(
        extract_precision_constraints(proposition),
        excerpts,
    )
    return proposition_tokens <= excerpt_tokens and not invented_precision


def apply_semantic_judge_batch(
    audits: list[dict[str, Any]],
    semantic_judge: Callable[[dict[str, Any]], Any] | None,
    *,
    section_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply one primary semantic batch plus at most one targeted
    format-repair batch to ambiguous rows."""

    ambiguous = [
        item for item in audits
        if item.get("status") != "supported" and item.get("ranked_candidates")
    ]
    telemetry = {
        "enabled": semantic_judge is not None,
        "called": False,
        "batch_count": 0,
        "callable_call_count": 0,
        "call_count": 0,
        "api_call_count": 0,
        "ambiguous_component_count": len(ambiguous),
        "accepted_decision_count": 0,
        "rejected_decision_count": 0,
        "provider": "",
        "actual_model": "",
        "model_provenance": "unavailable",
        "model_tier": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "token_provenance": "unavailable",
        "estimated_cost_cny": 0.0,
        "cost_provenance": "unavailable",
        "fallback_used": False,
        "one_batch_invariant": True,
        "improvement_attributed_to_semantic_judge": False,
        "usage": {},
        "error": "",
    }
    if semantic_judge is None or not ambiguous:
        return audits, telemetry
    slot_by_component = {
        item["component_id"]: f"R{index:02d}"
        for index, item in enumerate(ambiguous)
    }
    component_by_slot = {
        slot: component_id for component_id, slot in slot_by_component.items()
    }
    format_failure_history: list[dict[str, Any]] = []
    unresolved_failures: dict[str, dict[str, Any]] = {}

    def record_failure(failure: dict[str, Any]) -> None:
        format_failure_history.append(dict(failure))
        component_id = failure.get("component_id")
        if component_id:
            unresolved_failures[str(component_id)] = dict(failure)

    def build_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "schema_version": "research_harness.fresh_evidence_semantic_batch.v1",
            "section_id": section_id,
            "instructions": (
                "Judge only cited excerpts. Preserve numbers, order/size qualifiers, "
                "negation, formal comparisons, acronyms, and quoted entities. "
                "Return strict JSON."
            ),
            "components": [
                {
                    "slot": slot_by_component[item["component_id"]],
                    "component_id": item["component_id"],
                    "requested_component": item["requested_component"],
                    "precision_constraints": item.get("precision_constraints") or [],
                    "candidates": [
                        {
                            "chunk_id": row["chunk_id"],
                            "excerpt": row["excerpt"],
                            "score": row["score"],
                        }
                        for row in item.get("ranked_candidates") or []
                    ],
                }
                for item in items
            ],
            "required_output": {
                "decisions": [{
                    "slot": "string",
                    "status": "supported|partially_supported|unsupported",
                    "supported_proposition": "string",
                    "residual_precision": ["string"],
                    "cited_candidate_chunk_ids": ["string"],
                }],
            },
        }

    payload = build_payload(ambiguous)
    telemetry["called"] = True
    telemetry["batch_count"] = 1
    telemetry["callable_call_count"] = 1

    def merge_call_telemetry() -> None:
        raw = getattr(semantic_judge, "last_telemetry", None)
        if not isinstance(raw, dict):
            return
        def add_key(key: str) -> None:
            try:
                telemetry[key] = int(telemetry.get(key) or 0) + max(
                    0, int(raw.get(key) or 0)
                )
            except (TypeError, ValueError):
                pass

        add_key("call_count")
        add_key("api_call_count")
        add_key("input_tokens")
        add_key("output_tokens")
        try:
            telemetry["estimated_cost_cny"] = round(
                float(telemetry.get("estimated_cost_cny") or 0.0)
                + max(0.0, float(raw.get("estimated_cost_cny") or 0.0)),
                6,
            )
        except (TypeError, ValueError):
            pass
        for key in ("provider", "actual_model", "model_tier"):
            if not telemetry.get(key) and raw.get(key):
                telemetry[key] = raw[key]
        for key in ("model_provenance", "token_provenance", "cost_provenance"):
            current = str(telemetry.get(key) or "unavailable")
            incoming = str(raw.get(key) or "unavailable")
            if current in {"", "unavailable"}:
                telemetry[key] = incoming
            elif incoming not in {"", "unavailable"} and incoming != current:
                telemetry[key] = "mixed"
        telemetry["fallback_used"] = bool(
            telemetry.get("fallback_used")
            or raw.get("fallback_used")
            or raw.get("failure")
            or not raw.get("success", True)
        )
        usage = raw.get("usage")
        if isinstance(usage, dict):
            telemetry.setdefault("usage_history", []).append(dict(usage))
            telemetry["usage"] = dict(usage)
        # The logical one-primary-plus-one-repair budget is bounded by the
        # batch/callable counters.  ``api_call_count`` is physical provider
        # attempts (key rotation after 429, configured model fallback, etc.)
        # and is fully counted here but must not reject a valid review.
        telemetry["one_batch_invariant"] = bool(
            telemetry["batch_count"] <= 2
            and telemetry["callable_call_count"] <= 2
        )

    def fail_closed(message: str, *, rejected: int = 0) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        telemetry["rejected_decision_count"] = max(
            telemetry["rejected_decision_count"], rejected
        )
        telemetry["fallback_used"] = True
        telemetry["error"] = message
        telemetry["improvement_attributed_to_semantic_judge"] = False
        return audits, telemetry

    by_component = {item["component_id"]: item for item in ambiguous}

    def process_decisions(
        response: Any,
        *,
        repair_component_ids: set[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(response, dict) or set(response) != {"decisions"}:
            raise ValueError("semantic judge response must contain only a decisions array")
        decisions = response.get("decisions")
        if not isinstance(decisions, list):
            raise ValueError("semantic judge decisions must be an array")
        seen_components: set[str] = set()
        updates: dict[str, dict[str, Any]] = {}
        for decision in decisions:
            raw_decision = dict(decision) if isinstance(decision, dict) else decision
            if not isinstance(decision, dict):
                record_failure({"reason": "decision_is_not_an_object", "raw": raw_decision})
                continue
            slot = str(decision.get("slot") or "")
            component_id = component_by_slot.get(slot) or str(decision.get("component_id") or "")
            audit = by_component.get(component_id)
            if not audit or (
                repair_component_ids is not None
                and component_id not in repair_component_ids
            ):
                record_failure({
                    "slot": slot,
                    "component_id": component_id,
                    "reason": "unknown_or_missing_component_id",
                    "raw": raw_decision,
                })
                continue
            if component_id in seen_components:
                record_failure({
                    "slot": slot,
                    "component_id": component_id,
                    "reason": "duplicate_component_id",
                    "raw": raw_decision,
                })
                continue
            seen_components.add(component_id)
            status = str(decision.get("status") or "")
            residual_raw = decision.get("residual_precision")
            cited_raw = decision.get("cited_candidate_chunk_ids")
            if (
                status not in SUPPORT_STATES
                or not isinstance(residual_raw, list)
                or not all(isinstance(item, str) for item in residual_raw)
                or not isinstance(cited_raw, list)
                or not all(isinstance(item, str) for item in cited_raw)
            ):
                record_failure({
                    "slot": slot,
                    "component_id": component_id,
                    "reason": "invalid_decision_shape",
                    "raw": raw_decision,
                })
                continue
            candidate_by_chunk = {
                str(item.get("chunk_id") or ""): item
                for item in audit.get("ranked_candidates") or []
                if item.get("chunk_id")
            }
            cited_ids = _unique(cited_raw)
            if any(item not in candidate_by_chunk for item in cited_ids):
                record_failure({
                    "slot": slot,
                    "component_id": component_id,
                    "reason": "unknown_candidate_id",
                    "raw": raw_decision,
                })
                continue
            proposition = _clean(decision.get("supported_proposition"))
            cited_candidates = [candidate_by_chunk[item] for item in cited_ids]
            if status != "unsupported" and (
                not cited_ids or not _proposition_traceable(proposition, cited_candidates)
            ):
                record_failure({
                    "slot": slot,
                    "component_id": component_id,
                    "reason": "untraceable_proposition",
                    "raw": raw_decision,
                })
                continue
            cited_text = " ".join(item["excerpt"] for item in cited_candidates)
            missing_constraints = missing_precision_constraints(
                audit.get("precision_constraints") or [],
                cited_text,
            )
            if status == "supported" and (missing_constraints or residual_raw):
                status = "partially_supported"
            residual = normalize_residuals([
                *(_clean(item) for item in residual_raw if _clean(item)),
                *precision_residuals(missing_constraints),
            ])
            if status == "partially_supported" and not residual:
                residual = [proposition_coverage_residual(
                    audit.get("requested_component", "")
                )]
            if status == "unsupported":
                proposition = ""
                cited_ids = []
                cited_candidates = []
                residual = normalize_residuals(
                    residual or list(audit.get("residual_components") or [])
                )
            updates[component_id] = {
                "status": status,
                "support_state": status,
                "supported_component": proposition,
                "chunk_ids": cited_ids,
                "paper_ids": _unique(
                    item.get("paper_id") for item in cited_candidates if item.get("paper_id")
                ),
                "evidence_spans": [
                    {
                        "chunk_id": item["chunk_id"],
                        "quote": item["excerpt"],
                        "permission_ceiling": item.get("permission_ceiling", ""),
                        "content_depth": item.get("content_depth", ""),
                    }
                    for item in cited_candidates
                ],
                "residual_components": residual,
                "decision_source": "semantic_batch_judge",
            }
            unresolved_failures.pop(component_id, None)
        return updates

    proposed_updates: dict[str, dict[str, Any]] = {}
    response_error = ""
    raw_response: Any = None
    try:
        response = semantic_judge(payload)
    except Exception as exc:
        merge_call_telemetry()
        if isinstance(exc, (json.JSONDecodeError, ValueError, TypeError)):
            response_error = f"{type(exc).__name__}: {exc}"
        else:
            return fail_closed(f"{type(exc).__name__}: {exc}")
    else:
        # Merge exactly once per invocation.  Local JSON/shape parsing below
        # happens after this single merge and must not re-count the call.
        merge_call_telemetry()
        if not telemetry["one_batch_invariant"]:
            return fail_closed("semantic judge exceeded the one-batch call invariant")
        raw_response = response
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                response_error = f"{type(exc).__name__}: {exc}"
                response = None
        if response is not None:
            try:
                proposed_updates = process_decisions(response)
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                response_error = f"{type(exc).__name__}: {exc}"
            except Exception as exc:
                return fail_closed(f"{type(exc).__name__}: {exc}")

    for component_id in by_component:
        if (
            component_id not in proposed_updates
            and component_id not in unresolved_failures
        ):
            record_failure({
                "component_id": component_id,
                "slot": slot_by_component[component_id],
                "reason": response_error or "missing_decision",
                "raw": (
                    raw_response
                    if response_error
                    and isinstance(raw_response, (dict, list, str))
                    else None
                ),
            })

    repair_component_ids = {
        str(component_id)
        for component_id in unresolved_failures
        if component_id not in proposed_updates
    }
    if repair_component_ids:
        telemetry["batch_count"] = 2
        telemetry["callable_call_count"] = 2
        repair_items = [
            by_component[component_id]
            for component_id in sorted(repair_component_ids)
            if component_id in by_component
        ]
        repair_updates: dict[str, dict[str, Any]] = {}
        repair_error = ""
        raw_repair_response: Any = None
        history_before_repair = len(format_failure_history)
        try:
            repair_response = semantic_judge(build_payload(repair_items))
        except Exception as exc:
            merge_call_telemetry()
            repair_error = f"{type(exc).__name__}: {exc}"
            telemetry["fallback_used"] = True
            telemetry["error"] = repair_error
        else:
            merge_call_telemetry()
            raw_repair_response = repair_response
            if isinstance(repair_response, str):
                try:
                    repair_response = json.loads(repair_response)
                except (json.JSONDecodeError, ValueError, TypeError) as exc:
                    repair_error = f"{type(exc).__name__}: {exc}"
                    repair_response = None
            if repair_response is not None:
                try:
                    repair_updates = process_decisions(
                        repair_response,
                        repair_component_ids=repair_component_ids,
                    )
                except (json.JSONDecodeError, ValueError, TypeError) as exc:
                    repair_error = f"{type(exc).__name__}: {exc}"
                except Exception as exc:
                    repair_error = f"{type(exc).__name__}: {exc}"
            if repair_error:
                telemetry["fallback_used"] = True
                telemetry["error"] = repair_error
            proposed_updates.update(repair_updates)
        for component_id in sorted(repair_component_ids):
            if component_id in repair_updates:
                continue
            if component_id not in unresolved_failures:
                continue
            if any(
                item.get("component_id") == component_id
                for item in format_failure_history[history_before_repair:]
            ):
                continue
            record_failure({
                "component_id": component_id,
                "slot": slot_by_component[component_id],
                "reason": repair_error or "missing_decision",
                "raw": (
                    raw_repair_response
                    if repair_error
                    and isinstance(raw_repair_response, (dict, list, str))
                    else None
                ),
            })

    for component_id, update in proposed_updates.items():
        by_component[component_id].update(update)
    final_unresolved = {
        component_id: failure
        for component_id, failure in unresolved_failures.items()
        if component_id not in proposed_updates
    }
    for component_id, failure in final_unresolved.items():
        audit = by_component.get(component_id)
        if audit is None:
            continue
        audit["status"] = "unreviewed_format_failure"
        audit["support_state"] = "unsupported"
        audit["format_failure"] = failure.get("reason")
        audit["raw_format_payload"] = failure.get("raw")
        audit["decision_source"] = audit.get("decision_source", "unreviewed")
    telemetry["rejected_decision_count"] = len(final_unresolved)
    telemetry["accepted_decision_count"] = len(proposed_updates)
    telemetry["format_failures"] = list(final_unresolved.values())
    telemetry["format_failure_history"] = format_failure_history
    telemetry["improvement_attributed_to_semantic_judge"] = any(
        update.get("status") in {"supported", "partially_supported"}
        for update in proposed_updates.values()
    )
    return audits, telemetry
