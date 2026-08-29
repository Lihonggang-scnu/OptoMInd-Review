"""Deterministic convergence gate for the research-program mainline.

The review pipeline deliberately exposes several opportunities.  A research
program must choose one coherent spine before those opportunities become
hypotheses and work packages.  This module validates that decision without
calling an LLM and without judging whether a scientific hypothesis is true.
It only checks scope, compatibility, traceability, provenance permissions, and
the distinction between planned and executed work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional


PROJECT_TYPES = frozenset({"theory", "simulation", "experiment", "hybrid"})
_SINGLE_PLATFORM_TYPES = frozenset({"theory", "simulation", "experiment"})
_DEFERRED = "verification_deferred"
_PERMISSION_RANK = {
    "discovery_only": 0,
    "background_and_candidate_only": 1,
    "contextual_or_qualified_support": 1,
    "factual_support": 2,
}
_FOUNDATIONAL_PACKAGE_RE = re.compile(
    r"\b(?:literature|evidence|data|benchmark|prior[- ]art|"
    r"characteriz\w*|calibrat\w*|document\w*|material\w*|"
    r"source\s+audit|maturation|deposition)\w*\b",
    re.I,
)


def _items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def _strings(value: Any) -> list[str]:
    result: list[str] = []
    for item in _items(value):
        text = str(item or "").strip()
        if text:
            result.append(text)
    return list(dict.fromkeys(result))


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _tokens(value: Any) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z][a-z0-9_-]{2,}", _text(value).casefold())
        if token not in {"the", "and", "with", "from", "for", "that", "this"}
    }


def _allows_textual_evaluation_metrics(package: Mapping[str, Any]) -> bool:
    """Allow a foundational package to trace prose metrics without IDs.

    Preparatory literature/data packages can have meaningful evaluation
    criteria before the unified program metrics are defined.  They may use
    their existing ``evaluation_metrics`` as trace text, but this exception
    never creates or promotes a metric ID and does not apply to ordinary
    experimental packages.
    """

    if _strings(package.get("metric_ids")):
        return False
    evaluation_metrics = _strings(package.get("evaluation_metrics"))
    if not evaluation_metrics:
        return False
    corpus = " ".join(
        str(package.get(key) or "")
        for key in (
            "title",
            "objective",
            "methods",
            "inputs",
            "expected_outputs",
            "controls_or_baselines",
        )
    )
    return bool(_FOUNDATIONAL_PACKAGE_RE.search(corpus))


def _overlaps(left: Iterable[str], right: Iterable[str]) -> bool:
    left_tokens = _tokens(" ".join(str(item) for item in left))
    right_tokens = _tokens(" ".join(str(item) for item in right))
    return bool(left_tokens & right_tokens)


def _ids(rows: Iterable[Mapping[str, Any]], key: str) -> set[str]:
    return {
        str(row.get(key) or "").strip()
        for row in rows
        if isinstance(row, Mapping) and str(row.get(key) or "").strip()
    }


def _platform_component_types(platform: Mapping[str, Any]) -> set[str]:
    """Read explicit component types from a hybrid platform declaration."""

    raw = (
        platform.get("components")
        or platform.get("compatible_components")
        or platform.get("component_types")
        or []
    )
    values: list[Any]
    if isinstance(raw, Mapping):
        values = list(raw.keys())
    else:
        values = _items(raw)
    result: set[str] = set()
    for item in values:
        if isinstance(item, Mapping):
            value = item.get("project_type") or item.get("platform_type") or item.get("type") or item.get("kind")
        else:
            value = item
        value = _text(value).casefold()
        if value in _SINGLE_PLATFORM_TYPES:
            result.add(value)
    return result


@dataclass
class FocusGateResult:
    status: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == "passed" and not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "errors": list(dict.fromkeys(self.errors)),
            "warnings": list(dict.fromkeys(self.warnings)),
            "metrics": self.metrics,
        }


class ProgramFocusGate:
    """Validate the single-project spine before a plan is publishable."""

    schema_version = "research_harness.program_focus_gate.v1"

    @staticmethod
    def normalize_compatibility(gate: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
        """Apply only safe, downward platform corrections.

        A model sometimes labels a single simulation solver as ``hybrid``.
        That is a naming error, not a reason to spend another full model turn.
        We normalize it to the explicitly declared single platform.  A true
        hybrid remains hybrid only when its component types and component
        boundaries are explicit; otherwise validation fails closed.
        """

        normalized = dict(gate)
        platform = dict(normalized.get("shared_platform") or {})
        normalized["shared_platform"] = platform
        existing_audit = [
            item for item in _items(normalized.get("normalization_audit"))
            if isinstance(item, Mapping)
        ]
        # Historical audit rows describe earlier transitions; they are not
        # fresh corrections for this invocation.  Treating them as the
        # current correction list made every resume append the entire history
        # again, producing non-idempotent focus artifacts.
        corrections: list[dict[str, Any]] = []
        project_type = _text(normalized.get("project_type")).casefold()
        platform_type = _text(
            platform.get("platform_type") or platform.get("kind") or ""
        ).casefold()
        component_types = _platform_component_types(platform)
        if project_type == "hybrid":
            if platform_type in _SINGLE_PLATFORM_TYPES and not component_types:
                normalized["project_type"] = platform_type
                corrections.append(
                    {
                        "field": "project_type",
                        "from": "hybrid",
                        "to": platform_type,
                        "reason": "The shared platform declares one single-mode platform without hybrid components.",
                    }
                )
            elif len(component_types) == 1:
                only_type = next(iter(component_types))
                normalized["project_type"] = only_type
                platform["platform_type"] = only_type
                corrections.append(
                    {
                        "field": "project_type",
                        "from": "hybrid",
                        "to": only_type,
                        "reason": "The declared hybrid contains only one explicit platform component.",
                    }
                )
            elif len(component_types) >= 2:
                platform["platform_type"] = "hybrid"
        if corrections:
            normalized["normalization_audit"] = [
                *existing_audit,
                *corrections,
            ]
        elif existing_audit:
            normalized["normalization_audit"] = existing_audit
        return normalized, [dict(item) for item in corrections]

    def validate_focus_decision(
        self,
        gate: Mapping[str, Any],
        opportunities: Iterable[Mapping[str, Any]],
        hypotheses: Iterable[Mapping[str, Any]],
        *,
        shared_context: Optional[Mapping[str, Any]] = None,
        permission_map: Optional[Mapping[str, Mapping[str, str]]] = None,
    ) -> FocusGateResult:
        errors: list[str] = []
        warnings: list[str] = []
        opportunity_rows = [row for row in opportunities if isinstance(row, Mapping)]
        hypothesis_rows = [row for row in hypotheses if isinstance(row, Mapping)]
        opportunity_ids = _ids(opportunity_rows, "opportunity_id")
        hypothesis_ids = _ids(hypothesis_rows, "hypothesis_id")

        main_problem = gate.get("main_problem")
        if isinstance(main_problem, list):
            if len(main_problem) != 1:
                errors.append("main_problem_count_must_be_exactly_one")
            main_problem = main_problem[0] if len(main_problem) == 1 else {}
        if not isinstance(main_problem, Mapping) or not _text(main_problem.get("statement")):
            errors.append("main_problem_statement_missing")
        else:
            if not _text(main_problem.get("problem_id")):
                errors.append("main_problem_id_missing")

        project_type = _text(gate.get("project_type")).casefold()
        if project_type not in PROJECT_TYPES:
            errors.append("project_type_must_be_theory_simulation_experiment_or_hybrid")

        platform = gate.get("shared_platform")
        if not isinstance(platform, Mapping):
            errors.append("shared_platform_missing")
            platform = {}
        platform_id = _text(platform.get("platform_id"))
        compatibility_key = _text(
            platform.get("compatibility_key") or platform.get("platform_compatibility_key")
        )
        platform_type = _text(
            platform.get("platform_type") or platform.get("kind") or project_type
        ).casefold()
        boundaries = gate.get("boundaries") or gate.get("resource_boundaries")
        if not isinstance(boundaries, Mapping):
            errors.append("project_boundaries_missing")
            boundaries = {}
        if platform_type not in PROJECT_TYPES:
            errors.append("shared_platform_type_invalid")
        elif project_type in _SINGLE_PLATFORM_TYPES and platform_type != project_type:
            errors.append("shared_platform_type_incompatible_with_project_type")
        component_types = _platform_component_types(platform)
        if project_type == "hybrid":
            if platform_type != "hybrid":
                errors.append("hybrid_requires_explicit_hybrid_platform")
            if len(component_types) < 2:
                errors.append("hybrid_requires_at_least_two_compatible_components")
            component_boundaries = (
                platform.get("component_boundaries")
                or (boundaries.get("component_boundaries") if isinstance(boundaries, Mapping) else None)
            )
            if not isinstance(component_boundaries, Mapping):
                errors.append("hybrid_component_boundaries_missing")
            else:
                missing_component_boundaries = sorted(
                    component_type
                    for component_type in component_types
                    if not _strings(component_boundaries.get(component_type))
                )
                if missing_component_boundaries:
                    errors.append(
                        "hybrid_component_boundaries_missing:"
                        + ",".join(missing_component_boundaries)
                    )
        for key in ("platform_id", "name", "description", "compatibility_key"):
            if not _text(
                platform.get(key)
                or (platform.get("platform_compatibility_key") if key == "compatibility_key" else "")
            ):
                errors.append(f"shared_platform_{key}_missing")

        for key in ("personnel", "equipment", "data", "timeline", "budget"):
            if not _strings(boundaries.get(key)):
                errors.append(f"boundary_{key}_missing")

        evaluation = gate.get("unified_evaluation") or {}
        if not isinstance(evaluation, Mapping):
            errors.append("unified_evaluation_missing")
            evaluation = {}
        metrics = [row for row in _items(evaluation.get("metrics")) if row]
        baselines = [row for row in _items(evaluation.get("baselines")) if row]
        if not metrics:
            errors.append("unified_metrics_missing")
        if not baselines:
            errors.append("unified_baselines_missing")
        if not _text(evaluation.get("comparison_protocol")):
            errors.append("unified_comparison_protocol_missing")

        selected_opportunities = _strings(
            gate.get("selected_opportunity_ids") or gate.get("main_opportunity_ids")
        )
        selected_hypotheses = _strings(
            gate.get("main_hypothesis_ids") or gate.get("selected_hypothesis_ids")
        )
        future_branches = [
            row for row in _items(gate.get("future_branches")) if isinstance(row, Mapping)
        ]
        future_opportunities = {
            _text(row.get("opportunity_id"))
            for row in future_branches
            if _text(row.get("opportunity_id"))
        }
        future_hypotheses = set(
            _strings(gate.get("future_hypothesis_ids"))
        )
        for row in future_branches:
            if not bool(row.get("excluded_from_current_work_packages")):
                errors.append(
                    f"future_branch_not_explicitly_excluded:{row.get('opportunity_id')}"
                )
            if not _text(row.get("reason")):
                errors.append(f"future_branch_reason_missing:{row.get('opportunity_id')}")
        if not selected_opportunities:
            errors.append("selected_opportunity_ids_missing")
        if any(item not in opportunity_ids for item in selected_opportunities):
            errors.append("selected_opportunity_not_in_opportunity_map")
        if set(selected_opportunities) & future_opportunities:
            errors.append("selected_opportunity_also_declared_future_branch")
        if future_opportunities != opportunity_ids - set(selected_opportunities):
            errors.append("every_nonselected_opportunity_must_be_future_branch")
        if not 1 <= len(selected_hypotheses) <= 3:
            errors.append("main_hypothesis_count_must_be_1_to_3")
        if any(item not in hypothesis_ids for item in selected_hypotheses):
            errors.append("main_hypothesis_not_in_hypothesis_portfolio")
        if set(selected_hypotheses) & future_hypotheses:
            errors.append("main_hypothesis_also_declared_future")
        if future_hypotheses != hypothesis_ids - set(selected_hypotheses):
            errors.append("every_nonselected_hypothesis_must_be_future_or_selected")

        dependency_rows = [
            row
            for row in _items(gate.get("hypothesis_dependencies"))
            if isinstance(row, Mapping)
        ]
        selected_set = set(selected_hypotheses)
        incoming: dict[str, set[str]] = {item: set() for item in selected_set}
        outgoing: dict[str, set[str]] = {item: set() for item in selected_set}
        for row in dependency_rows:
            upstream = _text(
                row.get("upstream_hypothesis_id") or row.get("from_hypothesis_id")
            )
            downstream = _text(
                row.get("downstream_hypothesis_id") or row.get("to_hypothesis_id")
            )
            if upstream not in selected_set or downstream not in selected_set or upstream == downstream:
                errors.append("hypothesis_dependency_must_link_distinct_main_hypotheses")
                continue
            incoming[downstream].add(upstream)
            outgoing[upstream].add(downstream)
            if not _text(row.get("reason") or row.get("rationale")):
                errors.append("hypothesis_dependency_reason_missing")
        if len(selected_set) > 1:
            if not any(incoming.values()):
                errors.append("main_hypotheses_must_have_dependency_chain")
            roots = [item for item in selected_set if not incoming[item]]
            if len(roots) != 1:
                errors.append("main_hypotheses_must_have_one_spine_root")
            if roots:
                reachable: set[str] = set()
                stack = list(roots[:1])
                while stack:
                    node = stack.pop()
                    if node in reachable:
                        continue
                    reachable.add(node)
                    stack.extend(outgoing.get(node, set()))
                if reachable != selected_set:
                    errors.append("main_hypotheses_dependency_graph_disconnected")

        context = shared_context or {}
        required_context = (
            "review_scope_map",
            "literature_relation_graph",
            "technical_audit",
            "source_permissions",
        )
        if not isinstance(context, Mapping):
            errors.append("shared_phase3_r4_context_missing")
        else:
            for key in required_context:
                if not isinstance(context.get(key), Mapping) or not context.get(key):
                    errors.append(f"shared_context_{key}_missing")
            if not isinstance(context.get("r4_candidate_limitations"), list):
                errors.append("r4_candidate_limitations_missing")

        if permission_map:
            paper_permissions = permission_map.get("paper_permissions", {})
            chunk_permissions = permission_map.get("chunk_permissions", {})
            for row in opportunity_rows:
                # Future branches are explicitly excluded from the current
                # work packages.  Their weak metadata may remain useful for a
                # later literature search, but it must neither support the
                # selected program nor block an otherwise valid focus.  Only
                # opportunities promoted onto the current spine are subject
                # to this permission gate.
                if _text(row.get("opportunity_id")) not in set(
                    selected_opportunities
                ):
                    continue
                evidence_status = _text(row.get("evidence_status"))
                nonfactual = any(
                    _PERMISSION_RANK.get(str(paper_permissions.get(item)), -1) < 2
                    for item in _strings(row.get("supporting_paper_ids"))
                )
                if nonfactual and evidence_status not in {
                    "open_gap",
                    "partially_supported",
                }:
                    errors.append(
                        f"opportunity_uses_nonfactual_permission:{row.get('opportunity_id')}"
                    )
                elif nonfactual:
                    if not _text(row.get("author_inference")):
                        errors.append(
                            f"contextual_opportunity_missing_author_inference:{row.get('opportunity_id')}"
                        )
                    if not _text(row.get("uncertainty")):
                        errors.append(
                            f"contextual_opportunity_missing_uncertainty:{row.get('opportunity_id')}"
                        )
                    warnings.append(
                        f"opportunity_uses_contextual_permission:{row.get('opportunity_id')}"
                    )
            for row in hypothesis_rows:
                readiness = _text(row.get("readiness"))
                for chunk_id in _strings(row.get("supporting_chunk_ids")):
                    permission = str(chunk_permissions.get(chunk_id) or "")
                    if permission == "discovery_only" and readiness == "ready":
                        errors.append(
                            f"hypothesis_uses_discovery_only_chunk:{row.get('hypothesis_id')}"
                        )
                    if permission and _PERMISSION_RANK.get(permission, -1) < 2 and readiness == "ready":
                        errors.append(
                            f"ready_hypothesis_requires_factual_support:{row.get('hypothesis_id')}"
                        )

        metrics_names = [
            _text(row.get("metric_id") or row.get("name") if isinstance(row, Mapping) else row)
            for row in metrics
        ]
        baseline_names = [
            _text(row.get("baseline_id") or row.get("name") if isinstance(row, Mapping) else row)
            for row in baselines
        ]
        if not all(metrics_names):
            errors.append("unified_metrics_require_names_or_ids")
        if not all(baseline_names):
            errors.append("unified_baselines_require_names_or_ids")

        return FocusGateResult(
            status="passed" if not errors else "failed",
            errors=list(dict.fromkeys(errors)),
            warnings=list(dict.fromkeys(warnings)),
            metrics={
                "opportunity_count": len(opportunity_ids),
                "selected_opportunity_count": len(selected_opportunities),
                "future_branch_count": len(future_opportunities),
                "hypothesis_count": len(hypothesis_ids),
                "main_hypothesis_count": len(selected_hypotheses),
                "future_hypothesis_count": len(future_hypotheses),
                "project_type": project_type,
                "shared_platform_id": platform_id,
                "shared_platform_compatibility_key": compatibility_key,
                "shared_platform_component_types": sorted(component_types),
                "unified_metric_count": len(metrics),
                "unified_baseline_count": len(baselines),
            },
        )

    def validate_package(
        self,
        gate: Mapping[str, Any],
        opportunities: Iterable[Mapping[str, Any]],
        hypotheses: Iterable[Mapping[str, Any]],
        plan: Mapping[str, Any],
        *,
        shared_context: Optional[Mapping[str, Any]] = None,
        permission_map: Optional[Mapping[str, Mapping[str, str]]] = None,
    ) -> FocusGateResult:
        result = self.validate_focus_decision(
            gate,
            opportunities,
            hypotheses,
            shared_context=shared_context,
            permission_map=permission_map,
        )
        errors = list(result.errors)
        selected_opportunities = set(_strings(gate.get("selected_opportunity_ids")))
        selected_hypotheses = set(_strings(gate.get("main_hypothesis_ids")))
        future_opportunities = {
            _text(row.get("opportunity_id"))
            for row in _items(gate.get("future_branches"))
            if isinstance(row, Mapping)
        }
        future_hypotheses = set(_strings(gate.get("future_hypothesis_ids")))
        packages = [row for row in _items(plan.get("work_packages")) if isinstance(row, Mapping)]
        package_ids = {_text(row.get("work_package_id")) for row in packages}
        platform = gate.get("shared_platform") if isinstance(gate.get("shared_platform"), Mapping) else {}
        platform_id = _text(platform.get("platform_id"))
        compatibility_key = _text(platform.get("compatibility_key") or platform.get("platform_compatibility_key"))
        used_hypotheses: set[str] = set()
        used_opportunities: set[str] = set()
        for package in packages:
            package_id = _text(package.get("work_package_id"))
            linked_h = set(_strings(package.get("hypothesis_ids")))
            linked_o = set(_strings(package.get("opportunity_ids")))
            used_hypotheses.update(linked_h)
            used_opportunities.update(linked_o)
            if not (linked_h & selected_hypotheses or linked_o & selected_opportunities):
                errors.append(f"orphan_work_package:{package_id}")
            if linked_h & future_hypotheses:
                errors.append(f"future_hypothesis_leaks_into_work_package:{package_id}")
            if linked_o & future_opportunities:
                errors.append(f"future_branch_leaks_into_work_package:{package_id}")
            if _text(package.get("platform_id")) != platform_id:
                errors.append(f"work_package_platform_mismatch:{package_id}")
            if _text(package.get("platform_compatibility_key")) != compatibility_key:
                errors.append(f"work_package_platform_compatibility_mismatch:{package_id}")
            if str(package.get("verification_status") or "") != _DEFERRED:
                errors.append(f"work_package_verification_must_be_deferred:{package_id}")
            if not _strings(package.get("metric_ids")) and not _allows_textual_evaluation_metrics(package):
                errors.append(f"work_package_metric_ids_missing:{package_id}")
            if not _strings(package.get("baseline_ids")):
                errors.append(f"work_package_baseline_ids_missing:{package_id}")
            if set(_strings(package.get("metric_ids"))) and not set(_strings(package.get("metric_ids"))).issubset(
                {_text(row.get("metric_id") or row.get("name") if isinstance(row, Mapping) else row) for row in _items((gate.get("unified_evaluation") or {}).get("metrics"))}
            ):
                errors.append(f"work_package_uses_nonunified_metric:{package_id}")
            if set(_strings(package.get("baseline_ids"))) and not set(_strings(package.get("baseline_ids"))).issubset(
                {_text(row.get("baseline_id") or row.get("name") if isinstance(row, Mapping) else row) for row in _items((gate.get("unified_evaluation") or {}).get("baselines"))}
            ):
                errors.append(f"work_package_uses_nonunified_baseline:{package_id}")
        if used_hypotheses != selected_hypotheses:
            errors.append("main_hypothesis_not_fully_assigned_to_work_packages")
        if not selected_opportunities.issubset(used_opportunities):
            errors.append("selected_opportunity_not_fully_assigned_to_work_packages")
        if used_opportunities & future_opportunities:
            errors.append("future_branch_leak_detected")

        # When no factual premise exists, the first package must mature the
        # evidence base before it claims to validate the hypothesis.  This is
        # a deterministic project-order constraint, not a demand that a paper
        # already state the proposed hypothesis.
        if permission_map and packages:
            paper_permissions = permission_map.get("paper_permissions", {})
            chunk_permissions = permission_map.get("chunk_permissions", {})
            selected_hypothesis_rows = [
                row
                for row in hypotheses
                if isinstance(row, Mapping)
                and _text(row.get("hypothesis_id")) in selected_hypotheses
            ]
            no_factual_premise = False
            for row in selected_hypothesis_rows:
                factual_paper = any(
                    _PERMISSION_RANK.get(str(paper_permissions.get(paper_id)), -1) >= 2
                    for paper_id in _strings(row.get("supporting_paper_ids"))
                )
                factual_chunk = any(
                    _PERMISSION_RANK.get(str(chunk_permissions.get(chunk_id)), -1) >= 2
                    for chunk_id in _strings(row.get("supporting_chunk_ids"))
                )
                if not factual_paper and not factual_chunk:
                    no_factual_premise = True
                    break
            if no_factual_premise:
                first_package = packages[0]
                maturation_text = _text(
                    " ".join(
                        str(first_package.get(key) or "")
                        for key in (
                            "title",
                            "objective",
                            "methods",
                            "inputs",
                            "expected_outputs",
                            "controls_or_baselines",
                        )
                    )
                ).casefold()
                if not re.search(
                    r"\b(?:literature|evidence|data|benchmark|prior[- ]art|"
                    r"calibrat|corpus|source audit|maturation|feasibility)\w*\b",
                    maturation_text,
                ):
                    errors.append(
                        "first_work_package_must_mature_literature_data_or_benchmark_before_validation"
                    )

        matrix = [row for row in _items(gate.get("traceability_matrix")) if isinstance(row, Mapping)]
        if not matrix:
            errors.append("traceability_matrix_missing")
        matrix_h: set[str] = set()
        matrix_wp: set[str] = set()
        matrix_o: set[str] = set()
        for row in matrix:
            problem_id = _text(row.get("problem_id"))
            opportunity_id = _text(row.get("opportunity_id"))
            hypothesis_id = _text(row.get("hypothesis_id"))
            work_package_id = _text(row.get("work_package_id"))
            if problem_id != _text((gate.get("main_problem") or {}).get("problem_id")):
                errors.append("traceability_row_problem_mismatch")
            if opportunity_id not in selected_opportunities:
                errors.append(f"traceability_row_nonselected_opportunity:{opportunity_id}")
            if hypothesis_id not in selected_hypotheses:
                errors.append(f"traceability_row_nonselected_hypothesis:{hypothesis_id}")
            if work_package_id not in package_ids:
                errors.append(f"traceability_row_unknown_work_package:{work_package_id}")
            for key in (
                "proposed_tests",
                "metrics",
                "baselines",
                "falsification_conditions",
                "stop_or_pivot_decisions",
            ):
                if not _strings(row.get(key)):
                    errors.append(f"traceability_row_missing_{key}:{hypothesis_id}")
            matrix_h.add(hypothesis_id)
            matrix_wp.add(work_package_id)
            matrix_o.add(opportunity_id)
        if matrix_h != selected_hypotheses:
            errors.append("traceability_matrix_does_not_cover_main_hypotheses")
        if matrix_wp != package_ids:
            errors.append("traceability_matrix_does_not_cover_work_packages")
        if matrix_o != selected_opportunities:
            errors.append("traceability_matrix_does_not_cover_selected_opportunities")

        narrative = _text(plan.get("narrative_markdown"))
        problem_statement = _text((gate.get("main_problem") or {}).get("statement"))
        plan_problem = _text(plan.get("problem_statement") or plan.get("research_question"))
        if problem_statement and plan_problem and not (_tokens(problem_statement) & _tokens(plan_problem)):
            errors.append("plan_problem_does_not_match_focus_gate_problem")
        statements = {
            _text(row.get("hypothesis_id")): _text(row.get("statement"))
            for row in hypotheses
            if isinstance(row, Mapping) and _text(row.get("hypothesis_id")) in selected_hypotheses
        }
        missing_statements = [
            hypothesis_id
            for hypothesis_id, statement in statements.items()
            if statement and statement.casefold() not in narrative.casefold()
        ]
        if missing_statements:
            errors.append("full_main_hypothesis_statement_missing_from_narrative:" + ",".join(sorted(missing_statements)))

        limitations = list(
            _strings(
                (shared_context or {}).get("r4_candidate_limitations")
                if isinstance(shared_context, Mapping)
                else []
            )
        )
        if limitations and not _strings(plan.get("source_limitations")):
            errors.append("r4_candidate_limitations_not_carried_into_plan")
        if str(plan.get("results_status") or "") != _DEFERRED:
            errors.append("program_results_must_be_verification_deferred")

        result.metrics.update(
            {
                "work_package_count": len(packages),
                "traceability_row_count": len(matrix),
                "traceability_hypothesis_coverage": len(matrix_h),
                "traceability_work_package_coverage": len(matrix_wp),
                "narrative_hypothesis_statement_count": len(statements) - len(missing_statements),
                "r4_candidate_limitation_count": len(limitations),
            }
        )
        result.errors = list(dict.fromkeys([*errors]))
        result.status = "passed" if not result.errors else "failed"
        return result


__all__ = ["PROJECT_TYPES", "FocusGateResult", "ProgramFocusGate"]
