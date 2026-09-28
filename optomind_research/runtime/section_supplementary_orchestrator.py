"""Generic per-section automatic claim-evidence-gap closure orchestrator.

This module converts a frozen blueprint probe's important
``evidence_gap_records`` into durable version-agnostic ``claim_evidence_gap``
job specs, a fingerprint-pinned resumable run plan/state, and a thin report.

Execution is deliberately injectable: tests may bind fake callbacks, while
the default live path binds the existing production
``SupplementaryRetrievalPipeline`` + ``SupplementaryGapClosureCoordinator``
revalidator/revision components.  The orchestrator itself never calls a
network/model API and never mutates the probe or the base material cache.

One-wave policy: every selected claim receives at most one retrieval wave.
Progress stops the search; no-progress routes to claim revision/narrowing and
never to a second query.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .supplementary_gap_closure import (
    _task_and_registry_from_spec,
    build_claim_evidence_gap_specs_from_probe,
)
from .supplementary_retrieval_contract import (
    DEFAULT_EXPANSION_POLICIES,
    resolve_expansion_policy,
)

SCHEMA_VERSION = "optomind.section_supplementary_orchestrator.v1"
CLAIM_GAP_TYPE = "claim_evidence_gap"

STATUS_PENDING = "pending"
STATUS_COMMITTED = "committed"
STATUS_IMPROVED_STOP = "improved_stop"
STATUS_NO_PROGRESS = "no_progress"
STATUS_FAILED = "failed"
STATUS_DELETED = "deleted"
TERMINAL_STATUSES = frozenset({
    STATUS_COMMITTED,
    STATUS_IMPROVED_STOP,
    STATUS_NO_PROGRESS,
    STATUS_FAILED,
    STATUS_DELETED,
})
MAX_RETRIEVAL_WAVES = 1

PLAN_JSON = "section_supplementary_closure_plan.json"
STATE_JSON = "section_supplementary_closure_state.json"
REPORT_JSON = "section_supplementary_closure_report.json"

# Expanded retrieval intents that claim_evidence_gap must never enable.
_FORBIDDEN_EXPANSION_FLAGS = (
    "allow_role_expansion",
    "allow_batch_enrichment",
    "allow_reference_expansion",
    "allow_citation_expansion",
    "allow_recommendation_expansion",
    "allow_multi_seed_graph",
)

ADJUDICATION_DECISION_ACCEPT = "accept_reasoned_inference"
ADJUDICATION_DECISION_DIRECT_REVISION = "direct_revision"
ADJUDICATION_DECISION_RETRIEVAL = "retrieval"

_ADJUDICATOR_PROMPT_FALLBACK = (
    "You triage one claim-evidence gap before any retrieval wave. "
    "Choose accept_reasoned_inference only when exact supplied quotes "
    "support every premise and the conclusion is a cautious author synthesis "
    "with no new empirical, quantitative, or comparative fact. Choose "
    "direct_revision when qualifying, narrowing, or rewriting from current "
    "evidence is the fix, or when uncertain. Choose retrieval only for an "
    "indispensable missing factual unit. "
    'Return strict JSON: {"decision": "accept_reasoned_inference"|'
    '"direct_revision"|"retrieval", "reason": "...", '
    '"inference_rationale": "..."}'
)


def _load_prompt_text(filename: str, fallback: str) -> str:
    path = Path(__file__).resolve().parents[2] / "prompts" / filename
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return fallback


_ADJUDICATOR_SYSTEM_PROMPT = _load_prompt_text(
    "Section Supplementary Gap Worthiness Adjudicator.txt",
    _ADJUDICATOR_PROMPT_FALLBACK,
)


class SectionClosureError(RuntimeError):
    """Raised when the generic closure run cannot proceed safely."""


class ClosureResumeMismatchError(SectionClosureError):
    """Raised when resume state fingerprints no longer match the run inputs."""


class ClosureCallbackContractError(SectionClosureError):
    """Raised when an injected callback violates the outcome contract."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _probe_fingerprint(payload: Mapping[str, Any]) -> str:
    subset = {
        "schema_version": payload.get("schema_version"),
        "probe_timestamp": payload.get("probe_timestamp"),
        "section_id": payload.get("section_id"),
        "section_title": payload.get("section_title"),
        "research_context": payload.get("research_context"),
        "evidence_gap_records": payload.get("evidence_gap_records"),
        "final_claims": payload.get("final_claims"),
    }
    return _sha256_text(_canonical_json(subset))


def _config_fingerprint(
    *,
    include_nonblocking: bool,
    include_medium: bool,
    producer: str,
    expansion_policy_overrides: Mapping[str, Any] | None,
    component_ids: Sequence[str] | None,
    blocker_claim_ids: set[str] | None,
) -> str:
    return _sha256_text(_canonical_json({
        "include_nonblocking": bool(include_nonblocking),
        "include_medium": bool(include_medium),
        "producer": producer,
        "expansion_policy_overrides": dict(expansion_policy_overrides or {}),
        "component_ids": sorted(str(value) for value in (component_ids or [])),
        "blocker_claim_ids": sorted(blocker_claim_ids or []),
    }))


def _base_cache_fingerprint(base_cache_dir: str | Path | None) -> str | None:
    if base_cache_dir is None:
        return None
    root = Path(base_cache_dir)
    if not root.is_dir():
        raise SectionClosureError(
            f"base cache directory does not exist: {root}"
        )
    canonical_names = (
        "MATERIAL_UNITS_FINAL.json",
        "material_vectors.sqlite",
        "material_cache_manifest.json",
        "MERGE_MANIFEST.json",
    )
    digests: dict[str, str] = {}
    for name in canonical_names:
        path = root / name
        if path.is_file():
            digests[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return _sha256_text(_canonical_json(digests))


def _validate_base_cache_files(base_cache_dir: str | Path | None) -> None:
    if base_cache_dir is None:
        raise SectionClosureError(
            "live closure requires --base-cache-dir"
        )
    root = Path(base_cache_dir)
    missing = [
        name
        for name in ("MATERIAL_UNITS_FINAL.json", "material_vectors.sqlite")
        if not (root / name).is_file()
    ]
    if missing:
        raise SectionClosureError(
            f"baseline cache is incomplete; missing: {', '.join(missing)}"
        )


def _blocker_claim_ids_from_probe(payload: Mapping[str, Any]) -> set[str]:
    write_gate = payload.get("write_gate")
    write_gate = write_gate if isinstance(write_gate, Mapping) else {}
    candidates: list[Any] = []
    audit = write_gate.get("blueprint_claim_audit")
    if isinstance(audit, Mapping):
        candidates.append(audit.get("unadopted_unready_load_bearing_claim_ids"))
    candidates.append(write_gate.get("unadopted_unready_load_bearing_claim_ids"))
    candidates.append(write_gate.get("blocker_claim_ids"))
    blocker_ids: set[str] = set()
    for raw in candidates:
        if isinstance(raw, (list, tuple, set)):
            blocker_ids.update(str(value) for value in raw if str(value))
        elif raw:
            blocker_ids.add(str(raw))
    return blocker_ids


_EVALUATIVE_TOKENS = (
    "severely undermines",
    "undermines reliability",
    "raises concern",
    "is questionable",
    "fails to establish",
    "does not establish",
    "unconvincing",
    "not credible",
    "clearly insufficient",
    "merely rhetorical",
)
_FACTUAL_TOKEN_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|nm|um|μm|mm|cm|km|K|W|dB|eV|Hz|kg|m/s)\b"
    r"|\b(?:measured|observed|reported|demonstrated|achieved|increased|"
    r"decreased|compared|measured at)\b",
    re.I,
)


def make_default_task_worthiness_classifier() -> Callable[
    [Mapping[str, Any], Mapping[str, Any]],
    dict[str, Any],
]:
    """Deterministic conservative pre-retrieval worthiness classifier.

    Only clear non-factual/evaluative fragments are routed directly to
    revision; everything else stays retrievable (ambiguous defaults to
    retrieval in live mode).  No domain-specific vocabulary is used.
    """

    def classify(
        record: Mapping[str, Any],
        spec: Mapping[str, Any],
    ) -> dict[str, Any]:
        statement = str(
            record.get("claim_statement")
            or record.get("statement")
            or spec.get("claim_statement")
            or ""
        )
        missing = [
            str(value)
            for value in (record.get("missing_fact_units") or [])
            if str(value).strip()
        ]
        text = " ".join([statement, *missing]).casefold()
        if _FACTUAL_TOKEN_PATTERN.search(text):
            return {
                "decision": "retrieval",
                "reason": "contains concrete factual/quantitative content",
            }
        if (
            len(text.split()) <= 12
            and any(token in text for token in _EVALUATIVE_TOKENS)
        ):
            return {
                "decision": "direct_revision",
                "reason": (
                    "short evaluative/rhetorical fragment without factual "
                    "units; route to revision rather than literature search"
                ),
            }
        return {
            "decision": "ambiguous",
            "reason": (
                "not deterministically classified; defaults to retrieval "
                "unless an adjudicator is bound"
            ),
        }

    return classify


def _adjudication_payload(
    record: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    """Information-rich task fields only; never bulk raw retrieval content."""
    shared = spec.get("shared_context")
    shared = shared if isinstance(shared, Mapping) else {}
    try:
        _task, registry = _task_and_registry_from_spec(spec)
        registry_fields = registry.fields
    except Exception:
        registry_fields = {}
    reviewer_feedback = record.get("reviewer_feedback")
    reviewer_feedback = (
        reviewer_feedback if isinstance(reviewer_feedback, Mapping) else {}
    )
    current_evidence: list[dict[str, Any]] = []
    for item in record.get("current_evidence_summary") or []:
        if not isinstance(item, Mapping):
            continue
        bounded = dict(item)
        raw = str(bounded.get("raw_text") or bounded.get("text") or "")
        bounded["evidence"] = raw[:2000]
        current_evidence.append(bounded)
    topic_scope = (
        registry_fields.get("topic_scope")
        or record.get("topic_scope")
        or shared.get("topic_scope")
        or {}
    )
    topic_scope = topic_scope if isinstance(topic_scope, Mapping) else {}
    return {
        "user_question": (
            registry_fields.get("user_question")
            or record.get("user_question")
            or shared.get("user_question")
            or ""
        ),
        "topic_scope": topic_scope,
        "section_task": str(
            registry_fields.get("section_task")
            or record.get("section_task")
            or shared.get("section_task")
            or topic_scope.get("section_title")
            or ""
        ),
        "claim_role": str(
            registry_fields.get("claim_role")
            or record.get("claim_role")
            or ""
        ),
        "evidence_strength": str(
            record.get("evidence_strength")
            or record.get("required_evidence")
            or ""
        ),
        "sibling_verified_context": list(
            record.get("sibling_verified_context")
            or registry_fields.get("sibling_verified_context")
            or []
        ),
        "target_claim": registry_fields.get("target_claim_or_sentence")
        or record.get("target_claim_or_sentence")
        or {
            "claim_id": str(record.get("claim_id") or ""),
            "component_id": str(record.get("component_id") or ""),
            "statement": str(
                record.get("claim_statement")
                or record.get("statement")
                or ""
            ),
        },
        "current_evidence_summary": current_evidence,
        "bound_papers_and_quotes": list(
            registry_fields.get("bound_papers_and_quotes")
            or record.get("bound_papers_and_quotes")
            or []
        ),
        "failure_reason": str(
            record.get("failure_reason")
            or record.get("why_current_evidence_fails")
            or reviewer_feedback.get("failure_reason")
            or ""
        ),
        "reviewer_comments": list(
            reviewer_feedback.get("residual_reviewer_comments") or []
        ),
        "missing_fact_units": list(
            registry_fields.get("missing_fact_units")
            or record.get("missing_fact_units")
            or []
        ),
        "required_material_strength": dict(
            registry_fields.get("required_material_strength")
            or record.get("required_material_strength")
            or {}
        ),
        "success_criteria": list(
            registry_fields.get("retrieval_success_criteria")
            or record.get("success_criteria")
            or record.get("retrieval_success_criteria")
            or []
        ),
    }


def _parse_adjudication(content: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(str(content or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, Mapping):
        return None
    decision = str(parsed.get("decision") or "").strip().casefold()
    if decision not in {
        ADJUDICATION_DECISION_ACCEPT,
        ADJUDICATION_DECISION_DIRECT_REVISION,
        ADJUDICATION_DECISION_RETRIEVAL,
    }:
        return None
    return {
        "decision": decision,
        "reason": str(parsed.get("reason") or "").strip(),
        "inference_rationale": str(
            parsed.get("inference_rationale") or ""
        ).strip(),
    }


def _safe_adjudication_decision(
    adjudication: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize an adjudicator result; invalid decisions never retrieve."""

    decision = str(
        adjudication.get("decision") or ""
    ).strip().casefold()
    if decision not in {
        ADJUDICATION_DECISION_ACCEPT,
        ADJUDICATION_DECISION_DIRECT_REVISION,
        ADJUDICATION_DECISION_RETRIEVAL,
    }:
        return {
            **dict(adjudication),
            "decision": ADJUDICATION_DECISION_DIRECT_REVISION,
            "reason": (
                "adjudicator_invalid_decision_default_direct_revision"
            ),
            "inference_rationale": "",
        }
    return dict(adjudication)


def make_qwen_task_worthiness_adjudicator(
    qwen_call: Callable[..., Any],
    *,
    model_tier: str = "c2_model",
) -> Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]:
    """Cheap-Qwen adjudicator for ambiguous claim gaps (c2_model, no thinking).

    Returns ``{"decision", "reason", "inference_rationale", "usage"}``.
    Parse/model failure defaults to direct revision so a model failure never
    launches costly retrieval automatically.
    """
    system = _ADJUDICATOR_SYSTEM_PROMPT

    def adjudicate(
        record: Mapping[str, Any],
        spec: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = _adjudication_payload(record, spec)
        try:
            result = qwen_call(
                "SectionSupplementaryClosureAdjudicator",
                [
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": json.dumps(
                            payload, ensure_ascii=False
                        ),
                    },
                ],
                model_tier=model_tier,
                temperature=0,
                response_format={"type": "json_object"},
                enable_thinking=False,
                stream=True,
                force_mock=False,
                max_retries=0,
                timeout_seconds=120,
                max_transport_key_candidates=1,
                accept_partial_stream=False,
                allow_model_fallback=False,
            )
            parsed = _parse_adjudication(
                str(result.get("content") or "")
                if isinstance(result, Mapping)
                else ""
            )
            usage = result.get("_llm_usage") or result.get("usage")
            usage = usage if isinstance(usage, Mapping) and usage else None
            if parsed is not None:
                return {
                    **parsed,
                    "adjudicator": "qwen_c2",
                    "model_tier": model_tier,
                    "usage": usage,
                }
        except Exception:
            pass
        return {
            "decision": ADJUDICATION_DECISION_DIRECT_REVISION,
            "reason": "adjudicator_model_failure_default_direct_revision",
            "inference_rationale": "",
            "adjudicator": "qwen_c2",
            "model_tier": model_tier,
            "usage": None,
        }

    return adjudicate


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                payload, handle, ensure_ascii=False, indent=2
            )
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SectionClosureError(
            f"cannot read {path}: {exc}"
        ) from exc
    if not isinstance(value, Mapping):
        raise SectionClosureError(f"{path} must be a JSON object")
    return dict(value)


def _load_probe(probe_report: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(probe_report, Mapping):
        return copy.deepcopy(dict(probe_report))
    path = Path(probe_report)
    return _load_json(path)


def _validate_expansion_policy(policy: Mapping[str, Any]) -> None:
    for flag in _FORBIDDEN_EXPANSION_FLAGS:
        if bool(policy.get(flag)):
            raise SectionClosureError(
                f"claim_evidence_gap policy must not enable {flag}"
            )
    if int(policy.get("graph_seed_cap") or 0) != 0:
        raise SectionClosureError(
            "claim_evidence_gap policy must keep graph_seed_cap=0"
        )


def _prepare_output_dir(
    output_dir: Path,
    *,
    resume: bool,
    allow_overwrite: bool,
) -> None:
    if output_dir.exists() and not (resume or allow_overwrite):
        raise FileExistsError(
            f"output directory already exists: {output_dir}; "
            "pass --allow-overwrite or --resume to reuse it explicitly"
        )
    output_dir.mkdir(parents=True, exist_ok=True)


def build_section_closure_plan(
    *,
    probe_report: str | Path | Mapping[str, Any],
    base_cache_dir: str | Path | None = None,
    include_nonblocking: bool = False,
    include_medium: bool = False,
    producer: str = "section_supplementary_orchestrator",
    expansion_policy_overrides: Mapping[str, Any] | None = None,
    component_ids: Sequence[str] | None = None,
    blocker_claim_ids: set[str] | None = None,
    task_worthiness_classifier: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, fingerprint-pinned closure plan (pure).

    Selects important ``requires_new_evidence`` claim gaps by default,
    converts them to version-agnostic job specs, validates the required
    context projection and the exact upgraded ``claim_evidence_gap``
    expansion policy, and returns a serializable plan with stable task ids.
    No network/model/cache writes occur here.
    """

    probe = _load_probe(probe_report)
    explicit_blockers = {
        str(value) for value in (blocker_claim_ids or set())
    }
    effective_blockers = (
        explicit_blockers
        if explicit_blockers
        else _blocker_claim_ids_from_probe(probe)
    )
    classifier = (
        task_worthiness_classifier
        or make_default_task_worthiness_classifier()
    )
    specs = build_claim_evidence_gap_specs_from_probe(
        probe,
        producer=producer,
        blocking_only=True,
        include_nonblocking=include_nonblocking,
        include_medium=include_medium,
        blocker_claim_ids=effective_blockers or None,
        component_ids=component_ids,
    )
    validated: list[dict[str, Any]] = []
    policy_dict = dict(
        DEFAULT_EXPANSION_POLICIES[CLAIM_GAP_TYPE].to_dict()
    )
    for spec in specs:
        task, _registry = _task_and_registry_from_spec(spec)
        if task.gap_type != CLAIM_GAP_TYPE:
            raise SectionClosureError(
                f"unexpected gap type {task.gap_type!r}"
            )
        policy = resolve_expansion_policy(task, expansion_policy_overrides)
        policy_dict = policy.to_dict()
        _validate_expansion_policy(policy_dict)
        record = spec.get("record") or {}
        component_id = str(
            record.get("component_id") or record.get("claim_id") or ""
        )
        if effective_blockers:
            blocking = component_id in effective_blockers
        else:
            blocking = bool(
                str(record.get("importance") or "").lower() == "high"
                and str(record.get("disposition") or "")
                == "requires_new_evidence"
            )
        spec["metadata"]["blocking"] = blocking
        spec["metadata"]["worthiness"] = classifier(record, spec)
        validated.append(copy.deepcopy(spec))
    probe_fingerprint = _probe_fingerprint(probe)
    config_fingerprint = _config_fingerprint(
        include_nonblocking=include_nonblocking,
        include_medium=include_medium,
        producer=producer,
        expansion_policy_overrides=expansion_policy_overrides,
        component_ids=component_ids,
        blocker_claim_ids=effective_blockers,
    )
    base_fingerprint = _base_cache_fingerprint(base_cache_dir)
    blocking = 0
    for spec in validated:
        record = spec.get("record") or {}
        if (
            bool(spec.get("metadata", {}).get("blocking"))
            and str(record.get("disposition") or "")
            == "requires_new_evidence"
        ):
            blocking += 1
    accept_reasoned_inference_ids = [
        str(spec.get("task_id") or "")
        for spec in validated
        if (spec.get("metadata", {}).get("worthiness") or {}).get(
            "decision"
        )
        == ADJUDICATION_DECISION_ACCEPT
    ]
    direct_revision_ids = [
        str(spec.get("task_id") or "")
        for spec in validated
        if (spec.get("metadata", {}).get("worthiness") or {}).get(
            "decision"
        )
        == ADJUDICATION_DECISION_DIRECT_REVISION
    ]
    retrieval_candidate_ids = [
        str(spec.get("task_id") or "")
        for spec in validated
        if (spec.get("metadata", {}).get("worthiness") or {}).get(
            "decision"
        )
        in {
            ADJUDICATION_DECISION_RETRIEVAL,
            "ambiguous",
        }
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "plan",
        "probe_fingerprint": probe_fingerprint,
        "config_fingerprint": config_fingerprint,
        "base_cache_dir": str(base_cache_dir) if base_cache_dir else None,
        "base_cache_fingerprint": base_fingerprint,
        "include_nonblocking": bool(include_nonblocking),
        "include_medium": bool(include_medium),
        "blocker_claim_ids": sorted(effective_blockers),
        "producer": producer,
        "expansion_policy": dict(
            DEFAULT_EXPANSION_POLICIES[CLAIM_GAP_TYPE].to_dict()
        ),
        "resolved_expansion_policy": policy_dict,
        "task_count": len(validated),
        "blocking_count": blocking,
        "nonblocking_count": len(validated) - blocking,
        "accept_reasoned_inference_task_ids": accept_reasoned_inference_ids,
        "direct_revision_task_ids": direct_revision_ids,
        "retrieval_candidate_task_ids": retrieval_candidate_ids,
        "task_ids": [spec["task_id"] for spec in validated],
        "specs": validated,
    }


def _new_state(plan: Mapping[str, Any], output_dir: Path) -> dict[str, Any]:
    tasks: dict[str, dict[str, Any]] = {}
    for spec in plan.get("specs") or []:
        record = spec.get("record") or {}
        metadata = spec.get("metadata") or {}
        worthiness = metadata.get("worthiness") or {}
        task_id = str(spec.get("task_id") or "")
        tasks[task_id] = {
            "task_id": task_id,
            "source_record_id": str(spec.get("source_record_id") or ""),
            "claim_id": str(record.get("claim_id") or ""),
            "component_id": str(record.get("component_id") or ""),
            "status": STATUS_PENDING,
            "retrieval_wave_count": 0,
            "query_fingerprint": None,
            "outcome": None,
            "revision_action": None,
            "revalidation_required": False,
            "worthiness": dict(worthiness),
            "direct_revision": (
                str(worthiness.get("decision") or "")
                == ADJUDICATION_DECISION_DIRECT_REVISION
            ),
            "accept_reasoned_inference": (
                str(worthiness.get("decision") or "")
                == ADJUDICATION_DECISION_ACCEPT
            ),
            "attempts": [],
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "output_dir": str(output_dir),
        "probe_fingerprint": plan.get("probe_fingerprint"),
        "config_fingerprint": plan.get("config_fingerprint"),
        "base_cache_fingerprint": plan.get("base_cache_fingerprint"),
        "include_nonblocking": bool(plan.get("include_nonblocking")),
        "producer": str(plan.get("producer") or ""),
        "tasks": tasks,
        "cache_merge": None,
        "resumed": False,
        "updated_at": _now(),
    }


def _load_state(
    output_dir: Path,
    plan: Mapping[str, Any],
    *,
    resume: bool,
) -> dict[str, Any]:
    path = output_dir / STATE_JSON
    if not resume or not path.is_file():
        return _new_state(plan, output_dir)
    state = _load_json(path)
    if str(state.get("schema_version") or "") != SCHEMA_VERSION:
        raise ClosureResumeMismatchError(
            "resume refused: state schema_version mismatch"
        )
    checks = {
        "probe_fingerprint": (
            state.get("probe_fingerprint"),
            plan.get("probe_fingerprint"),
        ),
        "config_fingerprint": (
            state.get("config_fingerprint"),
            plan.get("config_fingerprint"),
        ),
        "base_cache_fingerprint": (
            state.get("base_cache_fingerprint"),
            plan.get("base_cache_fingerprint"),
        ),
    }
    for label, (stored, current) in checks.items():
        if stored != current:
            raise ClosureResumeMismatchError(
                f"resume refused: {label} changed "
                f"({stored!r} != {current!r})"
            )
    prior_tasks = {
        str(task_id)
        for task_id in (state.get("tasks") or {}).keys()
    }
    planned_tasks = set(plan.get("task_ids") or [])
    if prior_tasks != planned_tasks:
        raise ClosureResumeMismatchError(
            "resume refused: selected task set changed "
            f"({len(prior_tasks)} prior != {len(planned_tasks)} planned)"
        )
    state["resumed"] = True
    state["updated_at"] = _now()
    return state


def _validate_outcome(
    context: Mapping[str, Any],
    outcome: Any,
) -> dict[str, Any]:
    if not isinstance(outcome, Mapping):
        raise ClosureCallbackContractError(
            "retrieval_wave_callback must return a mapping"
        )
    result = dict(outcome)
    task_id = str(result.get("task_id") or "")
    if task_id != str(context.get("task_id") or ""):
        raise ClosureCallbackContractError(
            f"callback task_id {task_id!r} != expected "
            f"{context.get('task_id')!r}"
        )
    claim_id = str(result.get("claim_id") or "")
    if claim_id != str(context.get("claim_id") or ""):
        raise ClosureCallbackContractError(
            f"callback claim_id {claim_id!r} != expected "
            f"{context.get('claim_id')!r}"
        )
    status = str(result.get("status") or "").strip()
    if status not in TERMINAL_STATUSES:
        raise ClosureCallbackContractError(
            f"callback returned non-terminal status {status!r}; "
            "expected one of " + ",".join(sorted(TERMINAL_STATUSES))
        )
    wave = int(result.get("wave") or context.get("wave") or 0)
    if wave > MAX_RETRIEVAL_WAVES:
        raise ClosureCallbackContractError(
            f"callback attempted retrieval wave {wave}; "
            f"maximum is {MAX_RETRIEVAL_WAVES}"
        )
    result["wave"] = wave
    return result


def _task_report_row(task_id: str, task: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "claim_id": task.get("claim_id"),
        "component_id": task.get("component_id"),
        "status": task.get("status"),
        "retrieval_wave_count": task.get("retrieval_wave_count"),
        "query_fingerprint": task.get("query_fingerprint"),
        "outcome": task.get("outcome"),
        "revision_action": task.get("revision_action"),
        "revalidation_required": task.get("revalidation_required"),
    }


def _build_report(
    *,
    mode: str,
    plan: Mapping[str, Any],
    state: Mapping[str, Any],
    revision_actions: Mapping[str, Any],
    cache_merge: Mapping[str, Any],
    usage_records: Sequence[Mapping[str, Any]],
    downstream_tasks: Sequence[Mapping[str, Any]] | None = None,
    notifications: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    tasks = state.get("tasks") or {}
    terminal_counts = {
        status: sum(
            1
            for task in tasks.values()
            if str(task.get("status") or "") == status
        )
        for status in sorted(TERMINAL_STATUSES)
    }
    cost = _aggregate_usage(usage_records)
    final_snapshot: str | None = None
    for _task_id, task in sorted(tasks.items()):
        snapshot = (task.get("outcome") or {}).get("snapshot_path")
        if snapshot:
            final_snapshot = str(snapshot)
    report_tasks = (
        [
            copy.deepcopy(task)
            for task in (downstream_tasks or [])
        ]
        if downstream_tasks is not None
        else [
            _task_report_row(task_id, task)
            for task_id, task in sorted(tasks.items())
        ]
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "probe_fingerprint": plan.get("probe_fingerprint"),
        "config_fingerprint": plan.get("config_fingerprint"),
        "base_cache_fingerprint": plan.get("base_cache_fingerprint"),
        "include_nonblocking": bool(plan.get("include_nonblocking")),
        "resumed": bool(state.get("resumed")),
        "output_snapshot": final_snapshot,
        "final_snapshot": final_snapshot,
        "selected_task_count": len(tasks),
        "terminal_counts": terminal_counts,
        "tasks": report_tasks,
        "revision_actions": dict(revision_actions),
        "cache_merge": dict(cache_merge),
        "cost": cost,
        "downstream_tasks": report_tasks,
        "notifications": [
            copy.deepcopy(item) for item in (notifications or [])
        ],
        "immutability": {
            "probe_unchanged": True,
            "base_cache_unchanged": True,
        },
        "updated_at": _now(),
    }


def _aggregate_usage(
    usage_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate provider-reported usage without inventing prices."""
    buckets = {
        "query_generation": [],
        "material_cards": [],
        "revalidation": [],
        "revision": [],
        "other": [],
    }
    for record in usage_records:
        if not isinstance(record, Mapping):
            continue
        stage = str(
            record.get("stage")
            or record.get("module")
            or record.get("task_type")
            or "other"
        ).casefold()
        if "query" in stage or "generation" in stage or "adjudicat" in stage:
            bucket = "query_generation"
        elif "card" in stage or "material" in stage or "embed" in stage:
            bucket = "material_cards"
        elif "revalid" in stage or "recheck" in stage:
            bucket = "revalidation"
        elif "revis" in stage or "author" in stage or "reviewer" in stage:
            bucket = "revision"
        else:
            bucket = "other"
        buckets[bucket].append(dict(record))

    def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "call_count": len(records),
            "input_tokens": sum(
                int(record.get("input_tokens") or 0) for record in records
            ),
            "output_tokens": sum(
                int(record.get("output_tokens") or 0) for record in records
            ),
            "estimated_cost_cny": round(
                sum(
                    float(record.get("estimated_cost_cny") or 0.0)
                    for record in records
                ),
                6,
            ),
        }

    all_records = [
        record
        for bucket in buckets.values()
        for record in bucket
    ]
    return {
        "currency": "CNY",
        "provider_usage_available": bool(all_records),
        "total_estimated_cost_cny": round(
            sum(
                float(record.get("estimated_cost_cny") or 0.0)
                for record in all_records
            ),
            6,
        ),
        "total_input_tokens": sum(
            int(record.get("input_tokens") or 0) for record in all_records
        ),
        "total_output_tokens": sum(
            int(record.get("output_tokens") or 0) for record in all_records
        ),
        "query_generation": summarize(buckets["query_generation"]),
        "material_cards": summarize(buckets["material_cards"]),
        "revalidation": summarize(buckets["revalidation"]),
        "revision": summarize(buckets["revision"]),
        "other": summarize(buckets["other"]),
        "provider_usage_records": all_records,
        "note": (
            "Aggregated from provider-reported usage where available; no "
            "prices are invented."
        ),
    }


def _final_snapshot_from_jobs(
    jobs: Sequence[Mapping[str, Any]],
) -> str | None:
    """Last non-empty cumulative snapshot across sequentially merged jobs."""
    final_snapshot: str | None = None
    for job in jobs:
        result = job.get("result") or {}
        snapshot = result.get("snapshot_path")
        if snapshot:
            final_snapshot = str(snapshot)
    return final_snapshot


def _collect_nested_usage(
    value: Any,
    stage: str,
    seen: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Collect provider-reported usage nested in results without duplicates."""
    if seen is None:
        seen = set()
    records: list[dict[str, Any]] = []
    if isinstance(value, Mapping):
        for key in ("usage", "model_usage"):
            usage = value.get(key)
            if isinstance(usage, Mapping) and usage:
                marker = _canonical_json(usage)
                if marker not in seen:
                    seen.add(marker)
                    records.append({"stage": stage, **dict(usage)})
        for item in value.values():
            records.extend(_collect_nested_usage(item, stage, seen))
    elif isinstance(value, list):
        for item in value:
            records.extend(_collect_nested_usage(item, stage, seen))
    return records


def _collect_revision_usage(value: Any) -> list[dict[str, Any]]:
    """Collect provider-reported usage from revision/reviewer results."""
    return _collect_nested_usage(value, "revision")


def _record_reviewer_comments(record: Mapping[str, Any]) -> list[str]:
    feedback = record.get("reviewer_feedback")
    feedback = feedback if isinstance(feedback, Mapping) else {}
    comments = list(feedback.get("residual_reviewer_comments") or [])
    for key in ("failure_reason", "why_current_evidence_fails"):
        text = str(feedback.get(key) or "").strip()
        if text and text not in comments:
            comments.append(text)
    return comments[:20]


def _normalize_revision_results(
    revision_result: Any,
) -> list[dict[str, Any]]:
    """Normalize a revision callback result to a list of per-target dicts."""

    if not isinstance(revision_result, Mapping):
        return []
    raw_results = revision_result.get("results")
    if isinstance(raw_results, list):
        items = [
            dict(item)
            for item in raw_results
            if isinstance(item, Mapping)
        ]
    elif (
        revision_result.get("action")
        or revision_result.get("next_action")
    ):
        items = [dict(revision_result)]
    else:
        return []
    normalized: list[dict[str, Any]] = []
    for item in items:
        entry = dict(item)
        if "action" in entry and "next_action" not in entry:
            entry["next_action"] = str(entry.get("action") or "")
        entry.setdefault("target_type", "claim")
        normalized.append(entry)
    return normalized


_NON_DELETE_REVISION_ACTIONS = frozenset(
    {"narrow", "qualify", "rewrite", "accept_reasoned_inference"}
)


def _non_delete_revision_eligible(
    revision_results: Sequence[Mapping[str, Any]],
) -> bool:
    """True only when every revision result is a non-delete scoped revision."""

    return bool(revision_results) and all(
        str(item.get("next_action") or "").strip().casefold()
        in _NON_DELETE_REVISION_ACTIONS
        and str(item.get("revised_claim") or "").strip()
        for item in revision_results
        if isinstance(item, Mapping)
    ) and all(isinstance(item, Mapping) for item in revision_results)


def _merge_revision_into_per_target(
    per_target: Sequence[Mapping[str, Any]],
    revision_results: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Fill revision fields into per-target results without overwriting."""

    merged: list[dict[str, Any]] = [
        dict(item) for item in per_target
    ]
    for revision_item in revision_results:
        if not isinstance(revision_item, Mapping):
            continue
        target_id = str(revision_item.get("target_id") or "")
        target_type = str(
            revision_item.get("target_type") or "claim"
        )
        if target_id:
            match = next(
                (
                    item
                    for item in merged
                    if str(item.get("target_id") or "") == target_id
                    and str(item.get("target_type") or "claim")
                    == target_type
                ),
                None,
            )
        else:
            match = next(
                (
                    item
                    for item in merged
                    if str(item.get("target_type") or "claim")
                    == target_type
                ),
                None,
            )
        if match is None:
            merged.append(dict(revision_item))
            continue
        for field, value in revision_item.items():
            if field not in match or field in {
                "next_action",
                "revised_claim",
                "inference_rationale",
            }:
                match[field] = value
    return merged


def _direct_accept_outcome(
    task: Mapping[str, Any],
    record: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    worthiness = task.get("worthiness") or {}
    per_target = [{
        "target_id": str(
            task.get("component_id") or task.get("claim_id") or ""
        ),
        "target_type": "claim",
        "progress": "improved",
        "next_action": ADJUDICATION_DECISION_ACCEPT,
        "reason": str(worthiness.get("reason") or ""),
        "inference_rationale": str(
            worthiness.get("inference_rationale") or ""
        ),
        "residual_reviewer_comments": _record_reviewer_comments(record),
    }]
    outcome = {
        "status": STATUS_IMPROVED_STOP,
        "reason": str(worthiness.get("reason") or ""),
        "inference_rationale": str(
            worthiness.get("inference_rationale") or ""
        ),
        "per_target_results": per_target,
    }
    return STATUS_IMPROVED_STOP, outcome


def _direct_revision_outcome(
    task: Mapping[str, Any],
    record: Mapping[str, Any],
    revision_result: Any,
) -> tuple[str, dict[str, Any]]:
    results = _normalize_revision_results(revision_result)
    per_target: list[dict[str, Any]] = []
    eligible = _non_delete_revision_eligible(results)
    for item in results:
        item.setdefault("target_id", str(task.get("component_id") or ""))
        item.setdefault(
            "residual_reviewer_comments",
            _record_reviewer_comments(record),
        )
        per_target.append(item)
    status = STATUS_IMPROVED_STOP if eligible else STATUS_NO_PROGRESS
    outcome = {
        "status": status,
        "per_target_results": per_target,
        "revision_results": results,
    }
    return status, outcome


_STATUS_TO_DOWNSTREAM = {
    STATUS_COMMITTED: "closed",
    STATUS_IMPROVED_STOP: "improved_stop",
    STATUS_NO_PROGRESS: "revision_required",
    STATUS_FAILED: "failed",
    STATUS_DELETED: "revision_required",
}


def _downstream_tasks_from_state(
    state: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Convert orchestrator state into gap_closure_downstream-compatible tasks."""
    spec_by_id = {
        str(spec.get("task_id") or ""): spec
        for spec in plan.get("specs") or []
    }
    tasks: list[dict[str, Any]] = []
    for task_id, task in sorted((state.get("tasks") or {}).items()):
        spec = spec_by_id.get(task_id) or {}
        outcome = task.get("outcome") or {}
        per_target = list(outcome.get("per_target_results") or [])
        worthiness_reason = (
            task.get("worthiness") or {}
        ).get("reason")
        if (
            task.get("accept_reasoned_inference")
            or task.get("direct_revision")
        ) and not per_target:
            revision_results = list(
                outcome.get("revision_results") or []
            )
            if revision_results:
                per_target = [
                    copy.deepcopy(item)
                    for item in revision_results
                    if isinstance(item, Mapping)
                ]
        if not per_target and (
            task.get("direct_revision")
            or task.get("accept_reasoned_inference")
        ):
            per_target = [{
                "target_id": task.get("component_id"),
                "target_type": "claim",
                "residual_reviewer_comments": (
                    [str(worthiness_reason)]
                    if worthiness_reason
                    else []
                ),
            }]
        status = str(task.get("status") or STATUS_PENDING)
        tasks.append({
            "task_id": task_id,
            "idempotency_key": (
                str(outcome.get("idempotency_key") or task_id)
            ),
            "component_id": task.get("component_id"),
            "claim_id": task.get("claim_id"),
            "gap_type": CLAIM_GAP_TYPE,
            "status": _STATUS_TO_DOWNSTREAM.get(
                status, "revision_required"
            ),
            "retrieval_wave_count": int(
                task.get("retrieval_wave_count") or 0
            ),
            "max_retrieval_waves": MAX_RETRIEVAL_WAVES,
            "next_action": (
                str(outcome.get("next_action") or "")
                or task.get("revision_action")
                or None
            ),
            "error": (
                copy.deepcopy(outcome.get("error"))
                if status == STATUS_FAILED
                else None
            ),
            "per_target_results": per_target,
            "snapshot_path": outcome.get("snapshot_path"),
            "snapshot_version": outcome.get("snapshot_version"),
            "revision_action": task.get("revision_action"),
            "revalidation_required": bool(
                task.get("revalidation_required")
            ),
            "record": copy.deepcopy(spec.get("record")),
        })
    return tasks


def _run_default_live_production(
    *,
    plan: Mapping[str, Any],
    output: Path,
    base_cache_dir: str | Path,
    state: Mapping[str, Any],
    cards_model_tier: str,
    results_limit: int | None,
    snippet_limit: int | None,
    qwen_call: Callable[..., Any] | None = None,
    task_worthiness_adjudicator: Callable[..., dict[str, Any]] | None = None,
    adjudicator_model_tier: str = "c2_model",
    semantic_ranker: Callable[..., Any] | None = None,
    embedder: Callable[..., Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Run the real one-wave loop via existing production components.

    Reuses the generic ``SupplementaryRetrievalPipeline`` and
    ``SupplementaryGapClosureCoordinator`` through the current-mainline
    adapter, so the default live CLI is a real closed loop.  Returns
    ``(updated_state, usage_records, notifications, production_report)``.
    """
    _validate_base_cache_files(base_cache_dir)
    from .supplementary_production_adapter import (
        DEFAULT_RECHECK_MODEL_TIER as RECHECK_MODEL_TIER,
        _resolve_qwen_call,
        load_material_units,
        make_pipeline,
        make_revalidator,
        make_revision_callback,
        _default_semantic_embedder,
        run_local_cache_evidence_preflight,
        validate_base_cache,
    )
    from optomind_research.runtime.supplementary_gap_closure import (
        SupplementaryGapClosureCoordinator,
    )
    from optomind_research.runtime.supplementary_retrieval_pipeline import (
        PipelineUsage,
    )

    base_units_path, base_vectors_path = validate_base_cache(base_cache_dir)
    effective_qwen_call = _resolve_qwen_call(qwen_call)
    effective_embedder = embedder or _default_semantic_embedder
    qwen_adjudicator = (
        task_worthiness_adjudicator
        or make_qwen_task_worthiness_adjudicator(
            effective_qwen_call,
            model_tier=adjudicator_model_tier,
        )
    )
    usage_records: list[dict[str, Any]] = []
    usage = PipelineUsage()
    pipeline = make_pipeline(
        output,
        results_limit=results_limit,
        snippet_limit=snippet_limit,
        cards_model_tier=cards_model_tier,
        usage=usage,
        qwen_call=effective_qwen_call,
    )
    job_contexts: dict[str, Any] = {}
    revalidator_fn = make_revalidator(
        job_contexts,
        qwen_call=effective_qwen_call,
        semantic_ranker=semantic_ranker,
        embedder=effective_embedder,
    )
    revision_callback_fn = make_revision_callback(
        job_contexts,
        qwen_call=effective_qwen_call,
        semantic_ranker=semantic_ranker,
        embedder=effective_embedder,
    )
    coordinator = SupplementaryGapClosureCoordinator(
        output / "coordinator.sqlite",
        base_units_path=base_units_path,
        base_vectors_path=base_vectors_path,
        snapshot_root=output / "long_term_material_cache",
        pipeline=pipeline,
        revalidator=revalidator_fn,
        revision_callback=revision_callback_fn,
    )
    try:
        baseline_unit_ids = {
            str(unit.get("unit_id") or "")
            for unit in load_material_units(base_units_path.parent)
        }
        adjudicated_specs: list[dict[str, Any]] = []
        for raw_spec in plan.get("specs") or []:
            spec = copy.deepcopy(raw_spec)
            worthiness = spec.get("metadata", {}).get("worthiness") or {}
            task_id = str(spec.get("task_id") or "")
            if worthiness.get("decision") == "ambiguous":
                resolved = qwen_adjudicator(
                    spec.get("record") or {}, spec
                )
                if isinstance(resolved, Mapping):
                    resolved = _safe_adjudication_decision(resolved)
                    adjudication_usage = resolved.get("usage")
                    if (
                        isinstance(adjudication_usage, Mapping)
                        and adjudication_usage
                    ):
                        usage_records.append({
                            "stage": "task_worthiness_adjudication",
                            **dict(adjudication_usage),
                        })
                    resolved = {
                        key: value
                        for key, value in resolved.items()
                        if key != "usage"
                    }
                    spec["metadata"]["worthiness"] = resolved
                    task = (state.get("tasks") or {}).get(task_id)
                    if isinstance(task, dict):
                        task["worthiness"] = copy.deepcopy(resolved)
                        task["direct_revision"] = (
                            str(resolved.get("decision") or "")
                            == ADJUDICATION_DECISION_DIRECT_REVISION
                        )
                        task["accept_reasoned_inference"] = (
                            str(resolved.get("decision") or "")
                            == ADJUDICATION_DECISION_ACCEPT
                        )
            adjudicated_specs.append(spec)
        for spec in adjudicated_specs:
            if (spec.get("metadata", {}).get("worthiness") or {}).get(
                "decision"
            ) != ADJUDICATION_DECISION_DIRECT_REVISION:
                continue
            task, registry = _task_and_registry_from_spec(spec)
            task_id = str(spec.get("task_id") or "")
            job_contexts[task_id] = {
                "spec": spec,
                "task": task,
                "registry": registry,
                "targets": list(spec.get("affected_targets") or []),
                "task_id": task.task_id,
                "baseline_unit_ids": baseline_unit_ids,
            }
        retrieval_specs = [
            spec
            for spec in adjudicated_specs
            if (spec.get("metadata", {}).get("worthiness") or {}).get(
                "decision"
            )
            not in {
                ADJUDICATION_DECISION_ACCEPT,
                ADJUDICATION_DECISION_DIRECT_REVISION,
            }
        ]
        for spec in retrieval_specs:
            task, registry = _task_and_registry_from_spec(spec)
            task_id = str(spec.get("task_id") or "")
            job_contexts[task_id] = {
                "spec": spec,
                "task": task,
                "registry": registry,
                "targets": list(spec.get("affected_targets") or []),
                "task_id": task.task_id,
                "baseline_unit_ids": baseline_unit_ids,
            }
        local_resolutions: dict[str, tuple[str, dict[str, Any]]] = {}
        local_outcomes: dict[str, dict[str, Any]] = {}
        local_snapshot_paths: dict[str, str] = {}
        network_jobs: dict[str, dict[str, Any]] = {}
        accumulated_snapshot: Path | None = None
        for spec in retrieval_specs:
            task, registry = _task_and_registry_from_spec(spec)
            task_id = str(spec.get("task_id") or "")
            record = spec.get("record") or {}
            component_id = str(
                record.get("component_id")
                or record.get("claim_id")
                or task_id
            )
            snapshot_path = accumulated_snapshot or Path(base_cache_dir)
            preflight = run_local_cache_evidence_preflight(
                registry.fields,
                load_material_units(snapshot_path),
                target_id=component_id,
                target_type="claim",
                qwen_call=effective_qwen_call,
                model_tier=RECHECK_MODEL_TIER,
                semantic_ranker=semantic_ranker,
                vectors_path=str(
                    snapshot_path / "material_vectors.sqlite"
                ),
                embedder=effective_embedder,
            )
            local_outcomes[task_id] = preflight
            local_snapshot_paths[task_id] = str(snapshot_path)
            if preflight.get("progress") in {"closed", "improved"}:
                if preflight.get("progress") == "closed":
                    per_target = [{
                        "target_id": component_id,
                        "target_type": "claim",
                        "progress": "closed",
                        "conclusion": preflight.get("conclusion"),
                        "reason": preflight.get("reason"),
                        "exact_quote_matches": preflight.get(
                            "exact_quote_matches"
                        ),
                        "locally_validated_quotes": preflight.get(
                            "locally_validated_quotes"
                        ),
                        "residual_reviewer_comments": (
                            _record_reviewer_comments(record)
                        ),
                    }]
                    outcome: dict[str, Any] = {
                        "status": STATUS_COMMITTED,
                        "per_target_results": per_target,
                        "local_cache": True,
                        "local_cache_snapshot_path": str(snapshot_path),
                        "snapshot_path": str(snapshot_path),
                        "snapshot_version": snapshot_path.name,
                        "local_cache_outcome": preflight,
                    }
                    local_resolutions[task_id] = (
                        STATUS_COMMITTED,
                        outcome,
                    )
                else:
                    per_target_map = {
                        (component_id, "claim"): preflight
                    }
                    try:
                        revision_result = revision_callback_fn(
                            job_key=task_id,
                            affected_targets=[(component_id, "claim")],
                            snapshot_path=str(snapshot_path),
                            per_target_results=per_target_map,
                        )
                        status, outcome = _direct_revision_outcome(
                            {"component_id": component_id},
                            record,
                            revision_result,
                        )
                        outcome["per_target_results"] = (
                            _merge_revision_into_per_target(
                                [dict(preflight)],
                                outcome.get("per_target_results") or [],
                            )
                        )
                        outcome["status"] = status
                        outcome["local_cache"] = True
                        outcome["local_cache_snapshot_path"] = str(
                            snapshot_path
                        )
                        outcome["snapshot_path"] = str(snapshot_path)
                        outcome["snapshot_version"] = snapshot_path.name
                        outcome["local_cache_outcome"] = preflight
                        local_resolutions[task_id] = (status, outcome)
                    except Exception as exc:
                        status = STATUS_NO_PROGRESS
                        outcome = {
                            "status": status,
                            "reason": (
                                f"local_revision_failed:{type(exc).__name__}"
                            ),
                            "per_target_results": [dict(preflight)],
                            "local_cache": True,
                            "local_cache_snapshot_path": str(snapshot_path),
                            "snapshot_path": str(snapshot_path),
                            "snapshot_version": snapshot_path.name,
                            "local_cache_outcome": preflight,
                        }
                        local_resolutions[task_id] = (status, outcome)
                continue
            job = coordinator.enqueue_gap_closure_spec(spec)
            job_contexts[str(job["idempotency_key"])] = {
                "spec": spec,
                "task": task,
                "registry": registry,
                "targets": list(spec.get("affected_targets") or []),
                "task_id": task.task_id,
                "baseline_unit_ids": baseline_unit_ids,
            }
            processed = coordinator.process_next()
            network_jobs[task_id] = processed
            snapshot = (processed.get("result") or {}).get("snapshot_path")
            if snapshot:
                accumulated_snapshot = Path(str(snapshot))
        notifications = coordinator.list_notifications()
        final_snapshot = _final_snapshot_from_jobs(
            list(network_jobs.values())
        )
        if final_snapshot is None and retrieval_specs:
            final_snapshot = str(
                accumulated_snapshot or Path(base_cache_dir)
            )
    finally:
        coordinator.close()

    for record in usage.query_generation:
        usage_records.append({"stage": "query_generation", **dict(record)})
    for record in usage.adjudication:
        usage_records.append({"stage": "adjudication", **dict(record)})
    for record in usage.material_cards:
        usage_records.append({"stage": "material_cards", **dict(record)})
    usage_records.append({
        "stage": "embedding",
        "input_tokens": int(usage.embedding.get("input_tokens") or 0),
        "request_count": int(usage.embedding.get("request_count") or 0),
        "estimated_cost_cny": 0.0,
    })
    revalidation_seen: set[str] = set()
    revision_seen: set[str] = set()

    updated_state = copy.deepcopy(dict(state))
    job_by_task_id = network_jobs
    for spec in adjudicated_specs:
        task_id = str(spec.get("task_id") or "")
        task = updated_state.get("tasks", {}).get(task_id)
        if task is None:
            continue
        worthiness = task.get("worthiness") or {}
        record = spec.get("record") or {}
        if task_id in local_resolutions:
            status, outcome = local_resolutions[task_id]
            task["status"] = status
            task["retrieval_wave_count"] = 0
            task["revalidation_required"] = False
            task["outcome"] = outcome
            if status in {STATUS_IMPROVED_STOP, STATUS_NO_PROGRESS}:
                task["revision_action"] = "local_cache_revision"
            task["attempts"] = list(task.get("attempts") or [])
            task["attempts"].append({
                "wave": 0,
                "status": status,
                "at": _now(),
                "detail": "local_cache_without_retrieval",
            })
            usage_records.extend(
                _collect_nested_usage(
                    outcome.get("local_cache_outcome") or {},
                    "revalidation",
                    revalidation_seen,
                )
            )
            usage_records.extend(
                _collect_nested_usage(
                    outcome.get("revision_results") or [],
                    "revision",
                    revision_seen,
                )
            )
            continue
        if worthiness.get("decision") == ADJUDICATION_DECISION_ACCEPT:
            status, outcome = _direct_accept_outcome(task, record)
            task["status"] = status
            task["retrieval_wave_count"] = 0
            task["revalidation_required"] = False
            task["outcome"] = outcome
            task["attempts"] = list(task.get("attempts") or [])
            task["attempts"].append({
                "wave": 0,
                "status": status,
                "at": _now(),
                "detail": "accept_reasoned_inference_without_retrieval",
            })
            continue
        if worthiness.get("decision") == ADJUDICATION_DECISION_DIRECT_REVISION:
            task["retrieval_wave_count"] = 0
            task["revalidation_required"] = False
            outcome: dict[str, Any] = {}
            try:
                revision_result = revision_callback_fn(
                    job_key=task_id,
                    affected_targets=[
                        (str(task.get("component_id") or ""), "claim")
                    ],
                    snapshot_path=None,
                )
                status, outcome = _direct_revision_outcome(
                    task, record, revision_result
                )
                task["status"] = status
                task["revision_action"] = "revision_callback_invoked"
                usage_records.extend(
                    _collect_nested_usage(
                        revision_result, "revision", revision_seen
                    )
                )
            except Exception as exc:
                task["status"] = STATUS_NO_PROGRESS
                task["revision_action"] = (
                    f"direct_revision_callback_failed:{exc}"
                )
                outcome = {
                    "status": STATUS_NO_PROGRESS,
                    "reason": str(worthiness.get("reason") or ""),
                    "per_target_results": [],
                }
            task["outcome"] = outcome
            task["attempts"] = list(task.get("attempts") or [])
            task["attempts"].append({
                "wave": 0,
                "status": task["status"],
                "at": _now(),
                "detail": "direct_revision_without_retrieval",
            })
            continue
        job = job_by_task_id.get(task_id) or {}
        status = str(job.get("status") or "")
        task["status"] = {
            "closed": STATUS_COMMITTED,
            "improved_stop": STATUS_IMPROVED_STOP,
            "revision_required": STATUS_NO_PROGRESS,
            "no_progress": STATUS_NO_PROGRESS,
            "still_open": STATUS_NO_PROGRESS,
            "failed": STATUS_FAILED,
        }.get(status, STATUS_NO_PROGRESS)
        task["retrieval_wave_count"] = int(
            job.get("retrieval_wave_count") or 0
        )
        task["revalidation_required"] = status in {
            "closed",
            "improved_stop",
        }
        result = job.get("result") or {}
        task["outcome"] = {
            "idempotency_key": job.get("idempotency_key"),
            "status": task["status"],
            "per_target_results": list(result.get("per_target_results") or []),
            "snapshot_path": final_snapshot,
            "snapshot_version": (
                result.get("snapshot_version") if final_snapshot else None
            ),
            "next_action": job.get("next_action"),
            "error": job.get("error"),
            "local_cache": False,
            "local_cache_snapshot_path": local_snapshot_paths.get(task_id),
            "local_cache_outcome": local_outcomes.get(task_id) or {},
        }
        revision = job.get("revision") or {}
        revision_results = (
            revision.get("results")
            if isinstance(revision, Mapping)
            else None
        )
        if revision_results:
            task["outcome"]["revision_results"] = revision_results
            task["revision_action"] = "revision_completed"
            usage_records.extend(
                _collect_nested_usage(
                    revision, "revision", revision_seen
                )
            )
        elif status in {"revision_required", "no_progress", "still_open"}:
            task["revision_action"] = "revision_requested_by_coordinator"
        task["attempts"] = list(task.get("attempts") or [])
        task["attempts"].append({
            "wave": task["retrieval_wave_count"],
            "status": task["status"],
            "at": _now(),
        })
        usage_records.extend(
            _collect_nested_usage(
                task.get("outcome", {}).get("per_target_results") or [],
                "revalidation",
                revalidation_seen,
            )
        )
    production_report = {
        "schema_version": SCHEMA_VERSION,
        "mode": "live_production",
        "output_snapshot": final_snapshot,
        "tasks": _downstream_tasks_from_state(updated_state, plan),
        "notifications": [
            dict(item) for item in notifications
        ],
        "provider_usage": {
            "pipeline": usage.to_dict(),
        },
    }
    return updated_state, usage_records, notifications, production_report


def run_section_supplementary_closure(
    *,
    probe_report: str | Path | Mapping[str, Any],
    output_dir: str | Path,
    base_cache_dir: str | Path | None = None,
    include_nonblocking: bool = False,
    include_medium: bool = False,
    dry_run: bool = True,
    resume: bool = False,
    allow_overwrite: bool = False,
    producer: str = "section_supplementary_orchestrator",
    expansion_policy_overrides: Mapping[str, Any] | None = None,
    component_ids: Sequence[str] | None = None,
    blocker_claim_ids: set[str] | None = None,
    task_worthiness_classifier: Callable[..., dict[str, Any]] | None = None,
    task_worthiness_adjudicator: Callable[..., dict[str, Any]] | None = None,
    adjudicator_model_tier: str = "c2_model",
    cards_model_tier: str = "c2_model",
    results_limit: int | None = None,
    snippet_limit: int | None = None,
    qwen_call: Callable[..., Any] | None = None,
    retrieval_wave_callback: Callable[[Mapping[str, Any]], Any] | None = None,
    revision_callback: Callable[[Mapping[str, Any], Mapping[str, Any]], Any]
    | None = None,
    merge_cache_callback: Callable[[Mapping[str, Any]], Any] | None = None,
    semantic_ranker: Callable[..., Any] | None = None,
    embedder: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run (or dry-validate) one bounded claim-evidence-gap closure wave.

    The default live binding uses the current generic adapter; injected
    callbacks remain available for deterministic tests and recovery.

    Dry-run writes plan/state/report artifacts only and never invokes
    retrieval/revision/cache callbacks.  Live mode uses the injected
    ``retrieval_wave_callback`` when provided; otherwise it binds the default
    generic production components.  Each claim gets at most
    one retrieval wave; ``no_progress`` routes to revision and never triggers
    a second search.
    """

    output = Path(output_dir)
    plan = build_section_closure_plan(
        probe_report=probe_report,
        base_cache_dir=base_cache_dir,
        include_nonblocking=include_nonblocking,
        include_medium=include_medium,
        producer=producer,
        expansion_policy_overrides=expansion_policy_overrides,
        component_ids=component_ids,
        blocker_claim_ids=blocker_claim_ids,
        task_worthiness_classifier=task_worthiness_classifier,
    )
    _prepare_output_dir(output, resume=resume, allow_overwrite=allow_overwrite)
    state = _load_state(output, plan, resume=resume)
    _atomic_write_json(output / PLAN_JSON, plan)
    _atomic_write_json(output / STATE_JSON, state)

    if dry_run:
        report = _build_report(
            mode="dry_run",
            plan=plan,
            state=state,
            revision_actions={},
            cache_merge={"status": "not_run"},
            usage_records=[],
        )
        _atomic_write_json(output / REPORT_JSON, report)
        return report

    if retrieval_wave_callback is None:
        updated_state, usage_records, notifications, production_report = (
            _run_default_live_production(
                plan=plan,
                output=output,
                base_cache_dir=base_cache_dir,
                state=state,
                cards_model_tier=cards_model_tier,
                results_limit=results_limit,
                snippet_limit=snippet_limit,
                qwen_call=qwen_call,
                task_worthiness_adjudicator=task_worthiness_adjudicator,
                adjudicator_model_tier=adjudicator_model_tier,
                semantic_ranker=semantic_ranker,
                embedder=embedder,
            )
        )
        state = updated_state
        _atomic_write_json(output / STATE_JSON, state)
        report = _build_report(
            mode="live",
            plan=plan,
            state=state,
            revision_actions={},
            cache_merge={"status": "production_coordinator"},
            usage_records=usage_records,
            downstream_tasks=production_report.get("tasks") or [],
            notifications=notifications,
        )
        _atomic_write_json(output / REPORT_JSON, report)
        return report

    revision_actions: dict[str, Any] = {}
    usage_records: list[dict[str, Any]] = []
    revision_seen: set[str] = set()
    tasks = state.get("tasks") or {}
    for spec in plan.get("specs") or []:
        task_id = str(spec.get("task_id") or "")
        task = tasks.get(task_id)
        if task is None:
            raise ClosureResumeMismatchError(
                f"state missing task {task_id}"
            )
        status = str(task.get("status") or STATUS_PENDING)
        if status in TERMINAL_STATUSES:
            if (
                status == STATUS_NO_PROGRESS
                and task.get("revision_action") is None
                and revision_callback is not None
            ):
                action = revision_callback(
                    {"task_id": task_id, "claim_id": task.get("claim_id")},
                    dict(task.get("outcome") or {}),
                )
                task["revision_action"] = action
                normalized = _normalize_revision_results(action)
                task["outcome"]["revision_results"] = normalized
                task["outcome"]["per_target_results"] = (
                    _merge_revision_into_per_target(
                        task["outcome"].get("per_target_results") or [],
                        normalized,
                    )
                )
                if _non_delete_revision_eligible(normalized):
                    task["status"] = STATUS_IMPROVED_STOP
                    task["outcome"]["status"] = STATUS_IMPROVED_STOP
                usage_records.extend(
                    _collect_nested_usage(
                        action, "revision", revision_seen
                    )
                )
                task["updated_at"] = _now()
                _atomic_write_json(output / STATE_JSON, state)
            continue
        worthiness = task.get("worthiness") or {}
        decision = str(worthiness.get("decision") or "")
        if (
            decision == "ambiguous"
            and task_worthiness_adjudicator is not None
        ):
            adjudication = task_worthiness_adjudicator(
                spec.get("record") or {},
                spec,
            )
            if isinstance(adjudication, Mapping):
                adjudication = _safe_adjudication_decision(adjudication)
                decision = str(
                    adjudication.get("decision")
                    or ADJUDICATION_DECISION_DIRECT_REVISION
                )
                worthiness = dict(worthiness, **dict(adjudication))
                task["worthiness"] = worthiness
                task["direct_revision"] = (
                    decision == ADJUDICATION_DECISION_DIRECT_REVISION
                )
                task["accept_reasoned_inference"] = (
                    decision == ADJUDICATION_DECISION_ACCEPT
                )
        if decision == ADJUDICATION_DECISION_ACCEPT:
            status, outcome = _direct_accept_outcome(task, spec.get("record") or {})
            task["status"] = status
            task["retrieval_wave_count"] = 0
            task["revision_action"] = None
            task["outcome"] = outcome
            task["attempts"] = list(task.get("attempts") or [])
            task["attempts"].append({
                "wave": 0,
                "status": status,
                "at": _now(),
                "detail": "accept_reasoned_inference_without_retrieval",
            })
            revision_actions[task_id] = (
                "accept_reasoned_inference_without_retrieval"
            )
            task["updated_at"] = _now()
            _atomic_write_json(output / STATE_JSON, state)
            continue
        if decision == ADJUDICATION_DECISION_DIRECT_REVISION:
            task["status"] = STATUS_NO_PROGRESS
            task["retrieval_wave_count"] = 0
            task["revision_action"] = "direct_revision_without_retrieval"
            task["outcome"] = {
                "status": STATUS_NO_PROGRESS,
                "reason": worthiness.get("reason"),
            }
            task["attempts"] = list(task.get("attempts") or [])
            task["attempts"].append({
                "wave": 0,
                "status": STATUS_NO_PROGRESS,
                "at": _now(),
                "detail": "direct_revision_without_retrieval",
            })
            if revision_callback is not None:
                action = revision_callback(
                    {
                        "task_id": task_id,
                        "claim_id": task.get("claim_id"),
                        "component_id": task.get("component_id"),
                    },
                    task["outcome"],
                )
                task["revision_action"] = action
                status, outcome = _direct_revision_outcome(
                    task,
                    spec.get("record") or {},
                    action,
                )
                task["status"] = status
                task["outcome"] = outcome
                task["outcome"]["revision_results"] = (
                    outcome.get("revision_results") or []
                )
                usage_records.extend(
                    _collect_nested_usage(
                        action, "revision", revision_seen
                    )
                )
                revision_actions[task_id] = action
            else:
                revision_actions[task_id] = "direct_revision_without_retrieval"
            task["updated_at"] = _now()
            _atomic_write_json(output / STATE_JSON, state)
            continue
        if int(task.get("retrieval_wave_count") or 0) >= MAX_RETRIEVAL_WAVES:
            raise SectionClosureError(
                f"task {task_id} already used its single retrieval wave"
            )
        context = {
            "task": copy.deepcopy(spec),
            "task_id": task_id,
            "claim_id": task.get("claim_id"),
            "component_id": task.get("component_id"),
            "wave": int(task.get("retrieval_wave_count") or 0) + 1,
            "probe_fingerprint": plan.get("probe_fingerprint"),
            "config_fingerprint": plan.get("config_fingerprint"),
            "base_cache_dir": str(base_cache_dir) if base_cache_dir else None,
            "base_cache_fingerprint": plan.get("base_cache_fingerprint"),
            "output_dir": str(output),
        }
        outcome = _validate_outcome(
            context, retrieval_wave_callback(context)
        )
        task["retrieval_wave_count"] = int(
            task.get("retrieval_wave_count") or 0
        ) + 1
        task["status"] = str(outcome.get("status") or "")
        task["query_fingerprint"] = outcome.get("query_fingerprint")
        task["outcome"] = outcome
        task["revalidation_required"] = bool(
            outcome.get("new_unit_ids")
            or outcome.get("status") in {STATUS_COMMITTED, STATUS_IMPROVED_STOP}
        )
        task["attempts"] = list(task.get("attempts") or [])
        task["attempts"].append({
            "wave": task["retrieval_wave_count"],
            "status": task["status"],
            "at": _now(),
            "detail": str(outcome.get("detail") or ""),
        })
        usage = outcome.get("usage")
        if isinstance(usage, Mapping):
            usage_records.append(dict(usage))
        if (
            task["status"] == STATUS_NO_PROGRESS
            and revision_callback is not None
        ):
            action = revision_callback(
                {
                    "task_id": task_id,
                    "claim_id": task.get("claim_id"),
                    "component_id": task.get("component_id"),
                },
                outcome,
            )
            task["revision_action"] = action
            normalized = _normalize_revision_results(action)
            task["outcome"]["revision_results"] = normalized
            task["outcome"]["per_target_results"] = (
                _merge_revision_into_per_target(
                    task["outcome"].get("per_target_results") or [],
                    normalized,
                )
            )
            if _non_delete_revision_eligible(normalized):
                task["status"] = STATUS_IMPROVED_STOP
                task["outcome"]["status"] = STATUS_IMPROVED_STOP
            usage_records.extend(
                _collect_nested_usage(
                    action, "revision", revision_seen
                )
            )
            revision_actions[task_id] = action
        elif task["status"] == STATUS_NO_PROGRESS:
            task["revision_action"] = "deferred_to_claim_revision"
            revision_actions[task_id] = "deferred_to_claim_revision"
        task["updated_at"] = _now()
        _atomic_write_json(output / STATE_JSON, state)

    cache_merge = state.get("cache_merge")
    if cache_merge is None or cache_merge.get("status") == "not_bound":
        cache_merge = {"status": "not_bound"}
    if (
        merge_cache_callback is not None
        and cache_merge.get("status") == "not_bound"
        and any(
            task.get("outcome", {}).get("new_unit_ids")
            for task in tasks.values()
        )
    ):
        merge_result = merge_cache_callback({
            "base_cache_dir": str(base_cache_dir) if base_cache_dir else None,
            "base_cache_fingerprint": plan.get("base_cache_fingerprint"),
            "output_dir": str(output),
            "tasks": [
                _task_report_row(task_id, task)
                for task_id, task in sorted(tasks.items())
            ],
        })
        cache_merge = (
            dict(merge_result) if isinstance(merge_result, Mapping)
            else {"status": "completed", "detail": str(merge_result)}
        )
        state["cache_merge"] = cache_merge
        _atomic_write_json(output / STATE_JSON, state)
    elif cache_merge.get("status") == "not_bound":
        state["cache_merge"] = cache_merge
        _atomic_write_json(output / STATE_JSON, state)

    report = _build_report(
        mode="live",
        plan=plan,
        state=state,
        revision_actions=revision_actions,
        cache_merge=cache_merge,
        usage_records=usage_records,
        downstream_tasks=_downstream_tasks_from_state(state, plan),
    )
    _atomic_write_json(output / REPORT_JSON, report)
    return report


__all__ = [
    "SCHEMA_VERSION",
    "CLAIM_GAP_TYPE",
    "STATUS_PENDING",
    "STATUS_COMMITTED",
    "STATUS_IMPROVED_STOP",
    "STATUS_NO_PROGRESS",
    "STATUS_FAILED",
    "STATUS_DELETED",
    "TERMINAL_STATUSES",
    "MAX_RETRIEVAL_WAVES",
    "PLAN_JSON",
    "STATE_JSON",
    "REPORT_JSON",
    "SectionClosureError",
    "ClosureResumeMismatchError",
    "ClosureCallbackContractError",
    "build_section_closure_plan",
    "run_section_supplementary_closure",
]
