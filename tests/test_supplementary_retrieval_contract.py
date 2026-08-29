"""Tests for the five-gap contract and shared fine-grained context registry."""

from __future__ import annotations

import copy

import pytest

from optomind_research.runtime.supplementary_retrieval_contract import (
    ABSTRACT_ONLY_BACKGROUND,
    CONTEXT_PROJECTION_SCHEMA_VERSION,
    CONTEXT_FIELD_CATALOG,
    DEFAULT_EXPANSION_POLICIES,
    DEFAULT_PORTFOLIO_LIMITS,
    GRAPH_EXPANSION_MODES,
    GAP_TYPE_REQUIRED_CONTEXT_FIELDS,
    GAP_TYPES,
    MATERIALIZATION_PRIORITY,
    STRUCTURE_GUARDRAILS,
    ContextRegistry,
    ContextValidationError,
    SupplementaryRetrievalTask,
    project_context_for_task,
    resolve_expansion_policy,
    task_fingerprint,
    validate_current_review_structure,
    validate_gap_type,
    validate_materialization_policy,
    validate_paper_introduction_conclusion_excerpts,
    validate_portfolio_limits,
    validate_task_context,
)


def _registry() -> ContextRegistry:
    registry = ContextRegistry()
    registry.set("user_question", "How do radiative cooling multilayers compare?")
    registry.set(
        "dynamic_axes",
        [{"axis_id": "Q01", "description": "multilayer mechanism"}],
    )
    registry.set(
        "section_task",
        {"section_id": "S01", "title": "Mechanism", "task": "Explain physics."},
    )
    registry.set(
        "target_claim_or_sentence",
        {"claim_id": "C1", "statement": "Measured cooling power exceeds 60 W/m2."},
    )
    registry.set("argument_role", "mechanism_explanation")
    registry.set(
        "bound_papers_and_quotes",
        [{"paper_id": "p1", "quote": "emissivity 0.95"}],
    )
    registry.set(
        "reviewer_feedback", {"mentor": "needs direct measurement support"}
    )
    registry.set("author_revision_history", [{"revision": 1, "outcome": "still_open"}])
    registry.set("missing_fact_units", ["cooling_power_measured", "spectral_range"])
    registry.set(
        "required_material_strength",
        {"minimum": "factual_support", "abstract_ceiling": "background_only"},
    )
    registry.set("retrieval_success_criteria", ["has_measured_cooling_power"])
    registry.set("existing_paper_identities", ["doi:10.1/example"])
    registry.set("historical_queries", [{"query_id": "h1", "text": "old query"}])
    registry.set("concurrent_queries", [{"query_id": "c1", "text": "active query"}])
    registry.set(
        "current_review_structure",
        {
            "existing_sections": [{"section_id": "S01"}],
            "new_sections": [],
            "new_subsections_per_existing_section": {},
        },
    )
    registry.set(
        "paper_introduction_conclusion_excerpts",
        {
            "current_paper_introduction_excerpt": "Radiative cooling is emerging.",
            "current_paper_conclusion_excerpt": "Fabrication challenges remain.",
        },
    )
    registry.set(
        "whole_review_feedback",
        {"section_count": 8, "uncovered_roles": ["boundary", "frontier"]},
    )
    registry.set(
        "visual_slots",
        [{"slot_id": "V01", "role": "mechanism_anchor", "section_id": "S01"}],
    )
    registry.set("visual_gaps", ["mechanism_anchor_figure_missing"])
    registry.set("topic_scope", {"topic": "radiative cooling multilayers"})
    registry.set(
        "materialization_policy",
        {
            "priority": list(MATERIALIZATION_PRIORITY),
            "abstract_background_only": True,
        },
    )
    registry.set("portfolio_limits", dict(DEFAULT_PORTFOLIO_LIMITS))
    return registry.freeze()


def _task(
    gap_type: str,
    *,
    task_id: str = "task-1",
    refs: tuple[str, ...] | None = None,
    **kwargs,
) -> SupplementaryRetrievalTask:
    kwargs.setdefault(
        "source_provenance", {"producer": "test", "stage": "unit"}
    )
    kwargs.setdefault("success_criteria", ("has_evidence",))
    kwargs.setdefault("material_requirements", ("s2_structured_body",))
    kwargs.setdefault(
        "retrieval_queries", ("radiative cooling multilayer inverse design",)
    )
    kwargs.setdefault("priority", 1)
    return SupplementaryRetrievalTask(
        task_id=task_id,
        gap_type=gap_type,
        context_refs=refs or GAP_TYPE_REQUIRED_CONTEXT_FIELDS[gap_type],
        **kwargs,
    )


def test_exactly_five_gap_types() -> None:
    assert len(GAP_TYPES) == 5
    assert set(GAP_TYPES) == {
        "claim_evidence_gap",
        "section_argument_gap",
        "review_structure_gap",
        "whole_review_gap",
        "visual_material_gap",
    }


def test_context_catalog_covers_required_field_cells() -> None:
    required_union = set().union(*(set(v) for v in GAP_TYPE_REQUIRED_CONTEXT_FIELDS.values()))
    assert required_union <= set(CONTEXT_FIELD_CATALOG)
    for field_id in (
        "user_question",
        "dynamic_axes",
        "section_task",
        "target_claim_or_sentence",
        "argument_role",
        "bound_papers_and_quotes",
        "reviewer_feedback",
        "author_revision_history",
        "missing_fact_units",
        "required_material_strength",
        "retrieval_success_criteria",
        "existing_paper_identities",
        "historical_queries",
        "concurrent_queries",
        "current_review_structure",
        "paper_introduction_conclusion_excerpts",
        "whole_review_feedback",
        "visual_slots",
        "visual_gaps",
        "topic_scope",
        "materialization_policy",
        "portfolio_limits",
    ):
        assert field_id in CONTEXT_FIELD_CATALOG


def test_required_context_subsets_differ_and_match_purpose() -> None:
    assert set(GAP_TYPE_REQUIRED_CONTEXT_FIELDS) == set(GAP_TYPES)
    required_sets = {
        gap_type: set(fields) for gap_type, fields in GAP_TYPE_REQUIRED_CONTEXT_FIELDS.items()
    }
    names = list(required_sets)
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            assert required_sets[left] != required_sets[right]
    claim = required_sets["claim_evidence_gap"]
    assert {
        "target_claim_or_sentence",
        "missing_fact_units",
        "required_material_strength",
        "bound_papers_and_quotes",
    } <= claim
    assert "section_task" not in claim
    section = required_sets["section_argument_gap"]
    assert {"section_task", "argument_role"} <= section
    assert "target_claim_or_sentence" not in section
    structure = required_sets["review_structure_gap"]
    assert {
        "current_review_structure",
        "paper_introduction_conclusion_excerpts",
    } <= structure
    assert "visual_slots" not in structure
    whole = required_sets["whole_review_gap"]
    assert {"whole_review_feedback", "portfolio_limits"} <= whole
    assert "materialization_policy" not in whole
    visual = required_sets["visual_material_gap"]
    assert {"visual_slots", "visual_gaps"} <= visual
    assert "section_task" not in visual


@pytest.mark.parametrize("alias", ["evidence_permission_deficit", "abstract_only"])
def test_evidence_permission_and_abstract_only_are_not_gap_types(alias: str) -> None:
    with pytest.raises(ValueError, match="not a gap type"):
        validate_gap_type(alias)
    with pytest.raises(ValueError, match="unknown gap type"):
        validate_gap_type("totally_unknown_gap")


def test_context_registry_rejects_unknown_field_wrong_type_and_mutation_after_freeze() -> None:
    registry = ContextRegistry()
    with pytest.raises(ContextValidationError, match="unknown_context_field"):
        registry.set("not_a_field", 1)
    with pytest.raises(TypeError, match="must be str"):
        registry.set("user_question", 123)
    frozen = registry.set("user_question", "question").freeze()
    with pytest.raises(RuntimeError, match="frozen"):
        frozen.set("user_question", "changed")


def test_from_dict_validates_catalog_ids_and_types() -> None:
    registry = _registry()
    payload = registry.to_dict()
    rehydrated = ContextRegistry.from_dict(payload)
    assert rehydrated._frozen is True
    assert rehydrated.resolve(("user_question",)) == registry.resolve(("user_question",))
    bad_id = dict(payload)
    bad_id["fields"] = {"not_a_catalog_field": 1}
    with pytest.raises(ContextValidationError, match="unknown_context_field"):
        ContextRegistry.from_dict(bad_id)
    bad_type = dict(payload)
    bad_type["fields"] = {"user_question": 123}
    with pytest.raises(TypeError, match="must be str"):
        ContextRegistry.from_dict(bad_type)


def test_deep_freeze_isolates_nested_mutation() -> None:
    registry = ContextRegistry()
    topic = {"topic": "radiative cooling"}
    registry.set("topic_scope", topic)
    registry.set("user_question", "question")
    frozen = registry.freeze()
    task = _task("claim_evidence_gap", refs=("topic_scope", "user_question"))
    fingerprint_before = task_fingerprint(task, frozen)
    topic["topic"] = "mutated"
    assert frozen.fields["topic_scope"] == {"topic": "radiative cooling"}
    assert task_fingerprint(task, frozen) == fingerprint_before


def test_missing_required_context_is_rejected() -> None:
    registry = _registry()
    task = SupplementaryRetrievalTask(
        task_id="task-missing",
        gap_type="claim_evidence_gap",
        context_refs=(
            "topic_scope",
            "user_question",
            "dynamic_axes",
            "bound_papers_and_quotes",
            "missing_fact_units",
            "required_material_strength",
            "retrieval_success_criteria",
            "existing_paper_identities",
            "materialization_policy",
        ),
    )
    errors = validate_task_context(task, registry)
    assert "missing_required_context:target_claim_or_sentence" in errors

    registry_missing = ContextRegistry()
    registry_missing.set("topic_scope", {"topic": "x"})
    registry_missing.set("user_question", "q")
    registry_missing.set("dynamic_axes", [])
    registry_missing.set("bound_papers_and_quotes", [])
    registry_missing.set("missing_fact_units", [])
    registry_missing.set("required_material_strength", {})
    registry_missing.set("retrieval_success_criteria", [])
    registry_missing.set("existing_paper_identities", [])
    registry_missing.set("materialization_policy", {})
    task_with_ref = SupplementaryRetrievalTask(
        task_id="task-missing-field",
        gap_type="claim_evidence_gap",
        context_refs=GAP_TYPE_REQUIRED_CONTEXT_FIELDS["claim_evidence_gap"],
    )
    errors = validate_task_context(task_with_ref, registry_missing)
    assert "missing_context_field:target_claim_or_sentence" in errors


def test_structure_guardrails_are_upper_bounds_not_quotas() -> None:
    base = {
        "existing_sections": [{"section_id": "S01"}],
        "new_sections": [{"title": "S1"}, {"title": "S2"}, {"title": "S3"}],
        "new_subsections_per_existing_section": {"S01": 3},
    }
    assert validate_current_review_structure(base) == []
    assert validate_current_review_structure({**base, "new_sections": []}) == []
    too_many_sections = {
        **base,
        "new_sections": [{"title": f"S{i}"} for i in range(4)],
    }
    assert any(
        "max_new_sections" in error
        for error in validate_current_review_structure(too_many_sections)
    )
    too_many_subsections = {
        **base,
        "new_subsections_per_existing_section": {"S01": 4},
    }
    assert any(
        "max_new_subsections" in error
        for error in validate_current_review_structure(too_many_subsections)
    )
    assert STRUCTURE_GUARDRAILS["max_new_sections"] == 3
    assert STRUCTURE_GUARDRAILS["max_new_subsections_per_existing_section"] == 3

    excerpts = {
        "current_paper_introduction_excerpt": "Intro.",
        "current_paper_conclusion_excerpt": "Conclusion.",
    }
    assert validate_paper_introduction_conclusion_excerpts(excerpts) == []
    errors = validate_paper_introduction_conclusion_excerpts(
        {"current_paper_introduction_excerpt": "", "current_paper_conclusion_excerpt": ""}
    )
    assert "missing_current_paper_introduction_excerpt" in errors
    assert "missing_current_paper_conclusion_excerpt" in errors


def test_task_validation_requires_contract_fields_and_route_rules() -> None:
    assert _task("claim_evidence_gap").validate() == []
    assert any("invalid_task_id" in error for error in _task("claim_evidence_gap", task_id="../bad").validate())
    assert any(
        "unknown gap type" in error
        for error in _task("not_a_gap", refs=("topic_scope",)).validate()
    )
    missing_requirements = _task(
        "claim_evidence_gap",
        success_criteria=(),
        material_requirements=(),
        source_provenance={},
    )
    errors = missing_requirements.validate()
    assert "success_criteria_must_not_be_empty" in errors
    assert "material_requirements_must_not_be_empty" in errors
    assert "source_provenance_must_be_non_empty_dict" in errors
    assert any(
        "visual_material_gap_requires_visual_route" in error
        for error in _task("visual_material_gap", visual_route=False).validate()
    )
    assert any(
        "visual_route_only_for_visual_material_gap" in error
        for error in _task("claim_evidence_gap", visual_route=True).validate()
    )


def test_fingerprint_stable_and_excludes_provenance_and_priority() -> None:
    registry = _registry()
    base = _task("claim_evidence_gap")
    other_provenance = _task(
        "claim_evidence_gap",
        source_provenance={"producer": "different", "stage": "other", "at": "2026"},
        priority=99,
    )
    assert task_fingerprint(base, registry) == task_fingerprint(other_provenance, registry)
    different_query = _task(
        "claim_evidence_gap",
        retrieval_queries=("a completely different query",),
    )
    assert task_fingerprint(base, registry) != task_fingerprint(different_query, registry)
    different_context = ContextRegistry()
    for field_id in CONTEXT_FIELD_CATALOG:
        different_context.set(field_id, copy.deepcopy(registry.fields[field_id]))
    different_context.fields["target_claim_or_sentence"]["statement"] = "changed"
    assert task_fingerprint(base, registry) != task_fingerprint(base, different_context)


def test_fingerprint_changes_with_expansion_policy_override_and_to_dict_includes_metadata() -> None:
    registry = _registry()
    base = _task("claim_evidence_gap")
    overridden = _task(
        "claim_evidence_gap",
        metadata={"expansion_policy": {"result_cap": 99}},
    )
    assert task_fingerprint(base, registry) != task_fingerprint(
        overridden, registry
    )
    assert resolve_expansion_policy(overridden).result_cap == 99
    assert base.to_dict()["metadata"] == {}
    assert overridden.to_dict()["metadata"] == {
        "expansion_policy": {"result_cap": 99}
    }


def test_portfolio_limits_are_selection_helpers() -> None:
    ok, violations = validate_portfolio_limits(200, 50)
    assert ok and violations == []
    ok, violations = validate_portfolio_limits(201, 50)
    assert not ok and any("max_references" in item for item in violations)
    ok, violations = validate_portfolio_limits(200, 51)
    assert not ok and any("background_fraction" in item for item in violations)
    ok, violations = validate_portfolio_limits(0, 0)
    assert ok and violations == []


def test_materialization_priority_and_abstract_rule() -> None:
    assert MATERIALIZATION_PRIORITY == (
        "s2_structured_body",
        "public_oa_fulltext",
        "abstract_claim",
    )
    assert ABSTRACT_ONLY_BACKGROUND is True
    assert validate_materialization_policy(
        {"priority": list(MATERIALIZATION_PRIORITY), "abstract_background_only": True}
    ) == []


def test_visual_route_flag() -> None:
    assert _task("visual_material_gap", visual_route=True).is_visual() is True
    assert _task("claim_evidence_gap", visual_route=True).is_visual() is True
    assert _task("claim_evidence_gap").is_visual() is False


def test_expansion_policy_defaults_per_gap_type() -> None:
    assert set(DEFAULT_EXPANSION_POLICIES) == set(GAP_TYPES)
    claim = DEFAULT_EXPANSION_POLICIES["claim_evidence_gap"]
    assert claim.result_cap == 16
    assert claim.extra_request_cap == 8
    assert claim.s2_snippet_results_per_query_cap == 10
    assert claim.s2_precise_paper_cap == 8
    assert claim.batch_enrichment_paper_cap == 0
    assert claim.oa_fulltext_paper_cap == 16
    assert claim.abstract_claim_paper_cap == 8
    assert claim.graph_seed_cap == 0
    assert claim.allow_role_expansion is False
    assert claim.allow_exact_paper_followup is True
    assert claim.allow_batch_enrichment is False
    assert claim.allow_oa_fulltext_fallback is True
    assert claim.allow_reference_expansion is False
    assert claim.allow_citation_expansion is False
    assert claim.allow_recommendation_expansion is False
    assert claim.allow_multi_seed_graph is False
    assert claim.allow_visual_processing is False
    assert claim.allow_graph_expansion is False
    assert claim.graph_modes == ()

    section = DEFAULT_EXPANSION_POLICIES["section_argument_gap"]
    assert section.result_cap == 16
    assert section.extra_request_cap == 12
    assert section.s2_snippet_results_per_query_cap == 10
    assert section.s2_precise_paper_cap == 12
    assert section.batch_enrichment_paper_cap == 24
    assert section.oa_fulltext_paper_cap == 16
    assert section.abstract_claim_paper_cap == 12
    assert section.graph_seed_cap == 2
    assert section.allow_role_expansion is True
    assert section.allow_exact_paper_followup is True
    assert section.allow_batch_enrichment is True
    assert section.allow_oa_fulltext_fallback is True
    assert section.allow_reference_expansion is True
    assert section.allow_citation_expansion is True
    assert section.allow_recommendation_expansion is False
    assert section.allow_multi_seed_graph is False
    assert set(section.graph_modes) == {"references", "citations", "cited_by"}

    structure = DEFAULT_EXPANSION_POLICIES["review_structure_gap"]
    assert structure.result_cap == 20
    assert structure.extra_request_cap == 20
    assert structure.s2_snippet_results_per_query_cap == 10
    assert structure.s2_precise_paper_cap == 24
    assert structure.batch_enrichment_paper_cap == 40
    assert structure.oa_fulltext_paper_cap == 32
    assert structure.abstract_claim_paper_cap == 24
    assert structure.graph_seed_cap == 3
    assert structure.allow_role_expansion is True
    assert structure.allow_exact_paper_followup is True
    assert structure.allow_batch_enrichment is True
    assert structure.allow_oa_fulltext_fallback is True
    assert structure.allow_reference_expansion is True
    assert structure.allow_citation_expansion is True
    assert structure.allow_recommendation_expansion is True
    assert structure.allow_multi_seed_graph is True
    assert structure.allow_visual_processing is False
    assert structure.allow_graph_expansion is True
    assert set(structure.graph_modes) == set(GRAPH_EXPANSION_MODES)

    whole = DEFAULT_EXPANSION_POLICIES["whole_review_gap"]
    assert whole.result_cap == 20
    assert whole.extra_request_cap == 14
    assert whole.s2_snippet_results_per_query_cap == 10
    assert whole.s2_precise_paper_cap == 16
    assert whole.batch_enrichment_paper_cap == 32
    assert whole.oa_fulltext_paper_cap == 24
    assert whole.abstract_claim_paper_cap == 16
    assert whole.graph_seed_cap == 1
    assert whole.allow_role_expansion is True
    assert whole.allow_exact_paper_followup is True
    assert whole.allow_batch_enrichment is True
    assert whole.allow_oa_fulltext_fallback is True
    assert whole.allow_reference_expansion is True
    assert whole.allow_citation_expansion is True
    assert whole.allow_recommendation_expansion is False
    assert whole.allow_multi_seed_graph is False

    visual = DEFAULT_EXPANSION_POLICIES["visual_material_gap"]
    assert visual.extra_request_cap == 8
    assert visual.s2_precise_paper_cap == 2
    assert visual.batch_enrichment_paper_cap == 0
    assert visual.oa_fulltext_paper_cap == 6
    assert visual.abstract_claim_paper_cap == 6
    assert visual.graph_seed_cap == 0
    assert visual.allow_visual_processing is True
    assert visual.allow_exact_paper_followup is True
    assert visual.allow_batch_enrichment is False
    assert visual.allow_oa_fulltext_fallback is True
    assert visual.allow_role_expansion is False
    assert visual.allow_reference_expansion is False
    assert visual.allow_citation_expansion is False
    assert visual.allow_recommendation_expansion is False
    assert visual.allow_multi_seed_graph is False
    assert visual.allow_graph_expansion is False

    for policy in DEFAULT_EXPANSION_POLICIES.values():
        assert policy.validate() == []


def test_approved_new_route_caps_are_exact() -> None:
    expected = {
        "claim_evidence_gap": (8, 0, 0, 16, 8),
        "section_argument_gap": (12, 24, 2, 16, 12),
        "review_structure_gap": (24, 40, 3, 32, 24),
        "whole_review_gap": (16, 32, 1, 24, 16),
        "visual_material_gap": (2, 0, 0, 6, 6),
    }
    for gap_type, (precise, batch, graph, oa, abstract) in expected.items():
        policy = DEFAULT_EXPANSION_POLICIES[gap_type]
        assert (
            policy.s2_precise_paper_cap,
            policy.batch_enrichment_paper_cap,
            policy.graph_seed_cap,
            policy.oa_fulltext_paper_cap,
            policy.abstract_claim_paper_cap,
        ) == (precise, batch, graph, oa, abstract)


def test_resolve_expansion_policy_independent_switches_and_overrides() -> None:
    task = _task("claim_evidence_gap", task_id="policy-base")
    base = resolve_expansion_policy(task)
    assert base.result_cap == 16
    assert base.extra_request_cap == 8
    assert base.allow_role_expansion is False
    assert base.allow_graph_expansion is False

    override_task = _task(
        "claim_evidence_gap",
        task_id="policy-override",
        metadata={
            "expansion_policy": {
                "result_cap": 12,
                "allow_role_expansion": True,
            }
        },
    )
    resolved = resolve_expansion_policy(override_task)
    assert resolved.result_cap == 12
    assert resolved.allow_role_expansion is True
    # Independent switches: untouched controls keep their defaults.
    assert resolved.extra_request_cap == 8
    assert resolved.allow_graph_expansion is False
    assert resolved.allow_oa_fulltext_fallback is True

    direct = resolve_expansion_policy(
        task,
        overrides={
            "extra_request_cap": 8,
            "allow_batch_enrichment": True,
            "allow_reference_expansion": True,
            "allow_citation_expansion": True,
            "allow_recommendation_expansion": False,
            "allow_multi_seed_graph": False,
        },
    )
    assert direct.extra_request_cap == 8
    assert direct.allow_batch_enrichment is True
    assert direct.allow_reference_expansion is True
    assert direct.allow_citation_expansion is True
    assert direct.allow_recommendation_expansion is False
    assert direct.allow_multi_seed_graph is False
    assert direct.allow_graph_expansion is True
    assert direct.graph_modes == ("references", "citations", "cited_by")
    assert direct.result_cap == 16

    # Per-route caps are independent overrides.
    capped = resolve_expansion_policy(
        task,
        overrides={
            "s2_precise_paper_cap": 0,
            "oa_fulltext_paper_cap": 4,
            "graph_seed_cap": 2,
            "abstract_claim_paper_cap": 3,
        },
    )
    assert capped.s2_precise_paper_cap == 0
    assert capped.oa_fulltext_paper_cap == 4
    assert capped.graph_seed_cap == 2
    assert capped.abstract_claim_paper_cap == 3
    assert capped.s2_snippet_results_per_query_cap == 10
    assert capped.batch_enrichment_paper_cap == 0

    # Independent switches never bleed into each other.
    only_references = resolve_expansion_policy(
        task,
        overrides={"allow_reference_expansion": True},
    )
    assert only_references.allow_reference_expansion is True
    assert only_references.allow_citation_expansion is False
    assert only_references.allow_recommendation_expansion is False
    assert only_references.allow_multi_seed_graph is False
    assert only_references.graph_modes == ("references",)

    # Legacy coarse override maps to every relation direction.
    legacy = resolve_expansion_policy(
        task,
        overrides={"allow_graph_expansion": True},
    )
    assert legacy.allow_reference_expansion is True
    assert legacy.allow_citation_expansion is True
    assert legacy.allow_recommendation_expansion is True
    assert legacy.allow_multi_seed_graph is True
    assert legacy.allow_graph_expansion is True

    legacy_modes = resolve_expansion_policy(
        task,
        overrides={"graph_modes": ["citations"]},
    )
    assert legacy_modes.allow_citation_expansion is True
    assert legacy_modes.allow_reference_expansion is False
    assert legacy_modes.allow_recommendation_expansion is False
    assert legacy_modes.allow_multi_seed_graph is False

    # Independent per-route caps are not coupled to the emergency ceiling.
    # A tiny extra_request_cap must never starve a normal route.
    independent = resolve_expansion_policy(
        task,
        overrides={
            "extra_request_cap": 1,
            "s2_precise_paper_cap": 2,
            "oa_fulltext_paper_cap": 6,
            "abstract_claim_paper_cap": 3,
            "graph_seed_cap": 1,
        },
    )
    assert independent.extra_request_cap == 1
    assert independent.s2_precise_paper_cap == 2
    assert independent.oa_fulltext_paper_cap == 6
    assert independent.abstract_claim_paper_cap == 3
    assert independent.graph_seed_cap == 1
    assert independent.validate() == []

    with pytest.raises(ValueError, match="unknown_graph_expansion_mode"):
        # graph_modes with an unknown mode is still rejected via from_dict.
        from optomind_research.runtime.supplementary_retrieval_contract import (
            SupplementaryExpansionPolicy,
        )

        SupplementaryExpansionPolicy.from_dict({
            "gap_type": "claim_evidence_gap",
            "allow_graph_expansion": True,
            "graph_modes": ["not_a_real_mode"],
        })


def test_project_context_projects_only_task_fields_for_all_gap_types() -> None:
    registry = _registry()
    for gap_type in GAP_TYPES:
        task = _task(
            gap_type,
            task_id=f"project-{gap_type}",
            visual_route=(gap_type == "visual_material_gap"),
        )
        projected = project_context_for_task(task, registry)
        expected_ids = set(GAP_TYPE_REQUIRED_CONTEXT_FIELDS[gap_type])
        assert set(projected) - {"task_metadata"} == expected_ids
        metadata = projected["task_metadata"]
        assert metadata["schema_version"] == CONTEXT_PROJECTION_SCHEMA_VERSION
        assert metadata["gap_type"] == gap_type
        assert set(metadata["context_field_ids"]) == expected_ids
        assert metadata["expansion_policy"]["gap_type"] == gap_type
        assert metadata["expansion_policy"]["result_cap"] == (
            DEFAULT_EXPANSION_POLICIES[gap_type].result_cap
        )
        # Whole-registry cells outside the task subset must never leak.
        assert "historical_queries" not in projected
        assert "concurrent_queries" not in projected
