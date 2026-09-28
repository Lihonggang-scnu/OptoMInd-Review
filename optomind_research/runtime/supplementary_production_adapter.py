"""Current-mainline production adapters for one-wave supplementary closure.

This module deliberately contains only generic cache/retrieval/revision glue.
It replaces the historical topic-specific S04 runner without importing or
restoring that archived script.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .supplementary_retrieval_pipeline import (
    PipelineUsage,
    SupplementaryRetrievalPipeline,
)

DEFAULT_RECHECK_MODEL_TIER = "c2_model"
DEFAULT_RANKED_UNIT_LIMIT = 24


def _resolve_qwen_call(qwen_call: Callable[..., Any] | None) -> Callable[..., Any]:
    if qwen_call is not None:
        return qwen_call
    from llm.qwen_chat_client import call_qwen_chat

    return call_qwen_chat


def _default_semantic_embedder(
    texts: Sequence[str],
    *,
    usage_accumulator: dict[str, int] | None = None,
    **kwargs: Any,
) -> list[list[float]]:
    from .material_semantic_cache import dashscope_embedder

    return dashscope_embedder(texts, usage_accumulator=usage_accumulator)


def validate_base_cache(base_cache_dir: str | Path) -> tuple[Path, Path]:
    root = Path(base_cache_dir)
    paths = (root / "MATERIAL_UNITS_FINAL.json", root / "material_vectors.sqlite")
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise ValueError("baseline cache is incomplete; missing: " + ", ".join(missing))
    return paths


def make_pipeline(
    output_dir: Path,
    *,
    results_limit: int | None,
    snippet_limit: int | None,
    cards_model_tier: str,
    usage: PipelineUsage,
    qwen_call: Callable[..., Any] | None = None,
    **overrides: Any,
) -> SupplementaryRetrievalPipeline:
    return SupplementaryRetrievalPipeline(
        output_dir / "supplementary_tasks.sqlite",
        work_root=output_dir / "work",
        results_limit=results_limit,
        snippet_limit=snippet_limit,
        cards_model_tier=cards_model_tier,
        usage=usage,
        qwen_call=qwen_call,
        **overrides,
    )


def load_material_units(snapshot_path: str | Path) -> list[dict[str, Any]]:
    path = Path(snapshot_path)
    if path.is_file():
        path = path.parent
    units_path = path / "MATERIAL_UNITS_FINAL.json"
    if not units_path.is_file():
        return []
    try:
        payload = json.loads(units_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    units = payload.get("units") if isinstance(payload, Mapping) else []
    return [dict(unit) for unit in units or [] if isinstance(unit, Mapping)]


def _unit_text(unit: Mapping[str, Any]) -> str:
    return str((unit.get("durable_content") or {}).get("raw_text") or "")


def _unit_paper_id(unit: Mapping[str, Any]) -> str:
    identity = unit.get("identity") or {}
    return str(identity.get("paper_id") or identity.get("doi") or "").strip()


def _eligible(unit: Mapping[str, Any]) -> bool:
    if str(unit.get("unit_kind") or "") != "text_chunk":
        return False
    if not _unit_paper_id(unit) or not _unit_text(unit).strip():
        return False
    content = unit.get("durable_content") or {}
    quality = (unit.get("durable_content_card") or {}).get("content_quality") or {}
    depth = str(content.get("content_depth") or "").casefold()
    source = str(quality.get("source_kind") or "").casefold()
    return "abstract" not in depth and "abstract" not in source


def _phrases(registry: Mapping[str, Any]) -> list[str]:
    values = [str(value).strip() for value in registry.get("missing_fact_units") or []]
    values = [value for value in values if value]
    if values:
        return values
    statement = str((registry.get("target_claim_or_sentence") or {}).get("statement") or "").strip()
    return [statement] if statement else []


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _exact_matches(units: Sequence[Mapping[str, Any]], phrases: Sequence[str]) -> dict[str, list[str]]:
    matches: dict[str, list[str]] = {}
    for phrase in phrases:
        normalized = _norm(phrase)
        if not normalized:
            continue
        for unit in units:
            if normalized in _norm(_unit_text(unit)):
                matches.setdefault(phrase, []).append(str(unit.get("unit_id") or ""))
    return matches


def _lexical_rank(units: Sequence[Mapping[str, Any]], query: str) -> list[Mapping[str, Any]]:
    tokens = {token for token in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", _norm(query))}
    return sorted(
        units,
        key=lambda unit: (
            -len(tokens & set(re.findall(r"[a-z0-9][a-z0-9_-]{2,}", _norm(_unit_text(unit))))),
            str(unit.get("unit_id") or ""),
        ),
    )


def _validated_quotes(
    response: Mapping[str, Any],
    units: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    by_id = {str(unit.get("unit_id") or ""): unit for unit in units}
    try:
        payload = json.loads(str(response.get("content") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    result: list[dict[str, str]] = []
    for item in payload.get("unit_quotes") or []:
        if not isinstance(item, Mapping):
            continue
        unit_id = str(item.get("unit_id") or "")
        quote = str(item.get("quote") or "").strip()
        unit = by_id.get(unit_id)
        if not unit or not quote or _norm(quote) not in _norm(_unit_text(unit)):
            continue
        result.append({
            "unit_id": unit_id,
            "quote": quote,
            "paper_id": _unit_paper_id(unit),
            "chunk_id": str((unit.get("identity") or {}).get("chunk_id") or ""),
        })
    return result


def run_local_cache_evidence_preflight(
    registry: Mapping[str, Any],
    units: Sequence[Mapping[str, Any]],
    *,
    target_id: str,
    target_type: str,
    qwen_call: Callable[..., Any] | None = None,
    model_tier: str = DEFAULT_RECHECK_MODEL_TIER,
    semantic_ranker: Callable[..., Any] | None = None,
    vectors_path: str | Path | None = None,
    embedder: Callable[..., Any] | None = None,
    max_candidates: int = DEFAULT_RANKED_UNIT_LIMIT,
) -> dict[str, Any]:
    eligible = [unit for unit in units if _eligible(unit)]
    phrases = _phrases(registry)
    matches = _exact_matches(eligible, phrases)
    base = {
        "source": "local_cache",
        "target_id": str(target_id),
        "target_type": str(target_type),
        "exact_quote_matches": matches,
        "eligible_unit_count": len(eligible),
        "snapshot_unit_count": len(units),
        "locally_validated_quotes": [],
        "adjudication": None,
        "usage": None,
    }
    if phrases and all(phrase in matches for phrase in phrases):
        return {**base, "progress": "closed", "conclusion": "direct_support", "reason": "local cache exact quote evidence satisfies every required phrase", "ranking_audit": {"ranking_mode": "exact"}}

    claim = str((registry.get("target_claim_or_sentence") or {}).get("statement") or "")
    query = " ".join([claim, *phrases]).strip()
    ranking_mode = "lexical"
    ranked: list[Mapping[str, Any]] | None = None
    if semantic_ranker is not None:
        try:
            ranked = list(semantic_ranker(eligible, query_text=query) or [])
            ranking_mode = "semantic"
        except Exception:
            ranking_mode = "semantic_failed_lexical_fallback"
    if ranked is None:
        ranked = _lexical_rank(eligible, query)
    by_id = {str(unit.get("unit_id") or ""): unit for unit in eligible}
    candidates: list[Mapping[str, Any]] = []
    for unit in ranked:
        unit_id = str(unit.get("unit_id") or "")
        if unit_id in by_id and by_id[unit_id] not in candidates:
            candidates.append(by_id[unit_id])
        if len(candidates) >= max(1, int(max_candidates)):
            break
    audit = {"ranking_mode": ranking_mode, "candidate_count": len(eligible), "selected_count": len(candidates), "top_k": max(1, int(max_candidates))}
    if candidates and qwen_call is not None:
        payload = {
            "claim_statement": claim,
            "required_missing_fact_units": phrases,
            "candidate_units": [
                {"unit_id": str(unit.get("unit_id") or ""), "paper_id": _unit_paper_id(unit), "raw_text": _unit_text(unit)[:4000]}
                for unit in candidates
            ],
        }
        try:
            response = qwen_call(
                "S04GapClosureRecheck",
                [{"role": "system", "content": "Conservatively identify candidate verbatim quotes. Return JSON with conclusion, unit_quotes, reason."}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                model_tier=model_tier,
                temperature=0.0,
                response_format={"type": "json_object"},
                force_mock=False,
                allow_model_fallback=False,
                enable_thinking=False,
            )
            quotes = _validated_quotes(response, candidates)
            usage = dict(response.get("_llm_usage") or {}) if isinstance(response, Mapping) else {}
            if quotes:
                try:
                    parsed = json.loads(str(response.get("content") or ""))
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = {}
                conclusion = str(parsed.get("conclusion") or "reasoned_inference")
                if conclusion not in {"direct_support", "reasoned_inference"}:
                    conclusion = "reasoned_inference"
                return {**base, "progress": "improved", "conclusion": conclusion, "reason": "locally validated exact quote proposed by bounded rechecker", "locally_validated_quotes": quotes, "adjudication": parsed, "usage": usage, "ranking_audit": audit}
            base["local_preflight_error"] = ""
            base["usage"] = usage
        except Exception as exc:
            base["local_preflight_error"] = f"local_preflight_recheck_failed:{type(exc).__name__}"
    return {**base, "progress": "no_progress", "conclusion": "insufficient", "reason": "no locally verified exact evidence", "ranking_audit": audit}


def _revision_for_record(record: Mapping[str, Any], target_id: str, target_type: str) -> dict[str, Any]:
    feedback = record.get("reviewer_feedback") or {}
    suggestion = str(feedback.get("required_revision_or_qualification") or feedback.get("author_revision_suggestion") or "").strip()
    lowered = suggestion.casefold()
    action = "delete" if "delete" in lowered and not any(word in lowered for word in ("narrow", "rewrite", "qualify")) else ("rewrite" if "rewrite" in lowered else ("narrow" if "narrow" in lowered else "qualify"))
    claim = str((record.get("target_claim_or_sentence") or {}).get("statement") or record.get("claim_statement") or "").strip()
    return {"target_id": str(target_id), "target_type": str(target_type), "next_action": action, "revised_claim": suggestion or claim or f"qualify claim {target_id}", "residual_reviewer_comments": list(feedback.get("residual_reviewer_comments") or []), "reason": "deterministic_current_mainline_revision"}


def make_revalidator(job_contexts: dict[str, Mapping[str, Any]], **kwargs: Any) -> Callable[..., dict[str, Any]]:
    def revalidate(*, job_key: str, affected_targets: Sequence[tuple[str, str]], snapshot_path: str, retrieval_wave_count: int) -> dict[str, Any]:
        context = job_contexts.get(job_key) or {}
        registry = getattr(context.get("registry"), "fields", {}) or {}
        units = load_material_units(snapshot_path)
        results = [run_local_cache_evidence_preflight(registry, units, target_id=target_id, target_type=target_type, qwen_call=kwargs.get("qwen_call"), semantic_ranker=kwargs.get("semantic_ranker"), embedder=kwargs.get("embedder")) for target_id, target_type in affected_targets]
        return {"results": results}
    return revalidate


def make_revision_callback(job_contexts: dict[str, Mapping[str, Any]], **kwargs: Any) -> Callable[..., dict[str, Any]]:
    def revise(*, job_key: str, affected_targets: Sequence[tuple[str, str]], snapshot_path: str | None = None, per_target_results: Any = None) -> dict[str, Any]:
        context = job_contexts.get(job_key) or {}
        record = ((context.get("spec") or {}).get("record") or {})
        return {"results": [_revision_for_record(record, target_id, target_type) for target_id, target_type in affected_targets]}
    return revise


__all__ = [
    "DEFAULT_RECHECK_MODEL_TIER",
    "validate_base_cache",
    "make_pipeline",
    "load_material_units",
    "run_local_cache_evidence_preflight",
    "make_revalidator",
    "make_revision_callback",
    "_default_semantic_embedder",
    "_resolve_qwen_call",
]
