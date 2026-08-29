from __future__ import annotations

import json

import pytest

from optomind_research.blueprint_council import BlueprintCouncil
from optomind_research.blueprint_council import _unsupported_numeric_claims
from optomind_research.blueprint_tournament_judge import BlueprintTournamentJudge
from optomind_research.section_contract_designer import SectionContractDesigner
from optomind_research.section_contract_designer import _validate_contract_planning_boundaries
from optomind_research.full_review_orchestrator import FullReviewOrchestrator
from optomind_research.full_review_state import FullReviewState
from optomind_research.review_mentor_agent import ReviewMentorAgent
from optomind_research.blueprint_scientific_critic import (
    BlueprintScientificCritic,
    deterministic_blueprint_audit,
)


def _charter(question: str) -> dict:
    return {
        "central_question": question,
        "structural_goals": ["Synthesize evidence and expose justified boundaries."],
        "constraints": {"word_budget_total": 9000, "section_count_range": [8, 10]},
    }


def _candidates(question: str) -> tuple[dict, dict]:
    charter = _charter(question)
    result = BlueprintCouncil().generate_candidates(
        charter=charter,
        concept_map_summary={"top_labels": ["mechanisms", "measurements", "materials", "applications"]},
    )
    return charter, result


class TestF2BlueprintContract:
    def test_section_contract_rejects_quota_placeholder_and_new_number(self):
        section = {
            "section_title": "1550 nm filter mechanisms",
            "purpose": "Assess angular response.",
            "planned_thesis": {"text": "Assess 1550 nm filters."},
        }
        charter = {
            "title": "1550 nm review",
            "central_question": "How do 1550 nm filters behave?",
            "scope_statement": "Focus on 1550 nm.",
        }
        base = {
            "section_title": section["section_title"],
            "section_purpose": section["purpose"],
            "central_thesis": "Assess 1550 nm filters.",
            "required_evidence_roles": [],
            "expected_visual_roles": [],
            "forbidden_overclaims": [],
            "open_questions": [],
            "evidence_fallback_policy": {},
        }
        _validate_contract_planning_boundaries(base, section, charter)
        for invalid_role in (
            "At least five papers are required.",
            "Report a shift below <X nm>.",
            "Use evidence from a 1310 nm device.",
        ):
            bad = dict(base)
            bad["required_evidence_roles"] = [invalid_role]
            with pytest.raises(ValueError):
                _validate_contract_planning_boundaries(bad, section, charter)

    def test_mentor_accepts_string_concept_labels(self):
        summary = ReviewMentorAgent._planning_evidence_summary(
            {"concept_nodes": ["radiative balance", {"label": "solar reflectance"}], "cluster_count": 7}
        )
        assert summary["top_labels"] == ["radiative balance", "solar reflectance"]
        assert summary["cluster_count"] == 7

    def test_all_candidates_have_stable_ids_and_planned_theses(self):
        charter, result = _candidates("How do mechanisms and limitations shape radiative cooling?")
        assert {c["candidate_id"] for c in result["candidates"]} == {"BP-A", "BP-B", "BP-C"}
        assert result["mode"] == "deterministic_non_production"
        assert result["production"] is False
        for candidate in result["candidates"]:
            sections = candidate["sections"]
            assert 8 <= len(sections) <= 10
            assert [s["section_id"] for s in sections] == [f"S{i + 1:02d}" for i in range(len(sections))]
            assert sum(s["estimated_word_budget"] for s in sections) <= 9000
            assert sections[0]["section_role"] == "introduction"
            assert sections[-1]["section_role"] == "synthesis"
            assert all(s["section_role"] == "body" for s in sections[1:-1])
            for section in sections:
                assert section["planned_thesis"]["claim_status"] == "planned"
                assert section["planned_thesis"]["evidence_required"]
                assert section["key_questions"]
                assert section["scope_guardrails"]
            assert not _unsupported_numeric_claims(candidate, charter["central_question"])

    def test_comprehensive_charter_uses_publication_scale_defaults(self):
        from optomind_research.review_charter_agent import ReviewCharterAgent

        charter = ReviewCharterAgent().build_charter(
            user_query="Review radiative cooling.",
            concept_map_summary={"cluster_count": 14},
            domain="optical_science",
        )
        assert charter["mode"] == "deterministic_non_production"
        assert charter["production"] is False
        assert charter["constraints"]["review_scale"] == "comprehensive"
        assert charter["constraints"]["word_budget_total"] == 22000
        assert charter["constraints"]["section_count_range"] == [8, 10]
        assert charter["review_quality_contract"]["section_target_range"] == [8, 10]
        assert charter["constraints"]["reference_target_range"] == [80, 150]

    def test_scientific_critic_flags_premature_strong_thesis(self):
        blueprint = {
            "sections": [
                {"section_id": "S01", "section_role": "introduction", "planned_thesis": {"text": "Frame the question."}},
                {"section_id": "S02", "section_role": "body", "planned_thesis": {"text": "This is the irreducible universal limit."}},
                {"section_id": "S03", "section_role": "synthesis", "planned_thesis": {"text": "Integrate qualified findings."}},
            ]
        }
        issues = deterministic_blueprint_audit(blueprint)
        assert any(row["issue_type"] == "premature_strong_thesis" for row in issues)
        result = BlueprintScientificCritic(real_llm=False).review(
            charter={}, blueprint=blueprint
        )
        assert result["verdict"] == "revise"

    def test_scientific_critic_allows_user_scope_number_but_not_new_result(self):
        blueprint = {
            "sections": [
                {"section_id": "S01", "section_role": "introduction", "planned_thesis": {"text": "Frame a 1550 nm filter question."}},
                {"section_id": "S02", "section_role": "body", "planned_thesis": {"text": "Test whether performance remains above 90%."}},
                {"section_id": "S03", "section_role": "synthesis", "planned_thesis": {"text": "Integrate qualified findings."}},
            ]
        }
        issues = deterministic_blueprint_audit(blueprint, allowed_numbers={"1550"})
        numeric_sections = {
            row["section_id"] for row in issues if row["issue_type"] == "unverified_numeric_thesis"
        }
        assert "S01" not in numeric_sections
        assert "S02" in numeric_sections

    @pytest.mark.parametrize(
        "marker",
        ["fundamentally limited", "unavoidable", "cannot resolve", "requires a staged approach"],
    )
    def test_scientific_critic_flags_additional_premature_markers(self, marker):
        blueprint = {
            "sections": [
                {"section_id": "S01", "section_role": "introduction", "planned_thesis": {"text": "Frame the question."}},
                {"section_id": "S02", "section_role": "body", "planned_thesis": {"text": f"The design is {marker} by this mechanism."}},
                {"section_id": "S03", "section_role": "synthesis", "planned_thesis": {"text": "Integrate qualified findings."}},
            ]
        }
        issues = deterministic_blueprint_audit(blueprint)
        assert any(row["issue_type"] == "premature_strong_thesis" for row in issues)

    def test_scientific_critic_ignores_manuscript_section_numbers(self):
        blueprint = {
            "sections": [
                {"section_id": "S01", "section_role": "introduction", "planned_thesis": {"text": "Frame the question."}},
                {"section_id": "S02", "section_role": "body", "planned_thesis": {"text": "This proposition is synthesized in Section 9 and S09."}},
                {"section_id": "S03", "section_role": "synthesis", "planned_thesis": {"text": "Integrate findings."}},
            ]
        }
        issues = deterministic_blueprint_audit(blueprint)
        assert not any(row["issue_type"] == "unverified_numeric_thesis" for row in issues)

    def test_body_contract_forbids_reintroduction_and_mini_conclusion(self):
        charter, candidates = _candidates("How do mechanisms shape radiative cooling?")
        blueprint = candidates["candidates"][0]
        contracts = SectionContractDesigner().design_contracts(
            charter=charter, blueprint=blueprint
        )["contracts"]
        body = contracts[1]
        assert body["section_role"] == "body"
        guardrails = body["continuity_guardrails"]
        assert guardrails["opening_mode"] == "continue_without_reintroduction"
        assert "generic definition of the review topic" in guardrails["prohibited_reintroductions"]

    @pytest.mark.parametrize(
        ("question", "expected"),
        [
            ("How do mechanisms and limitations determine radiative cooling performance?", "BP-A"),
            ("How has understanding of photonic topological insulators evolved historically?", "BP-B"),
            ("Compare the principal types and approaches to optical neural networks.", "BP-C"),
        ],
    )
    def test_tournament_is_question_sensitive(self, question, expected):
        charter, result = _candidates(question)
        recommendation = BlueprintTournamentJudge().evaluate_and_recommend(
            candidates=result["candidates"], charter=charter
        )
        assert recommendation["mode"] == "deterministic_non_production"
        assert recommendation["production"] is False
        assert recommendation["authoritative_section_range"] == [8, 10]
        assert recommendation["selected_candidate_id"] == expected
        sections = recommendation["unified_blueprint"]["sections"]
        assert 8 <= len(sections) <= 10
        for section in sections:
            assert len(section["full_section_workplan"]) == len(sections)
            assert len(section["sibling_section_responsibilities"]) == len(sections) - 1
            assert section["must_not_cover"]
            assert section["handoff_from_previous"]
            assert section["handoff_to_next"]

    def test_human_override_changes_blueprint_content_not_only_id(self):
        charter, result = _candidates("How do mechanisms and limitations shape radiative cooling?")
        candidates = result["candidates"]
        recommendation = BlueprintTournamentJudge().evaluate_and_recommend(
            candidates=candidates, charter=charter
        )
        target = next(c for c in candidates if c["candidate_id"] != recommendation["selected_candidate_id"])
        final = BlueprintTournamentJudge.finalize(
            recommendation,
            override={"choice_id": target["candidate_id"], "notes": "Human preference."},
            candidates_by_id={c["candidate_id"]: c for c in candidates},
        )
        assert final["selected_candidate_id"] == target["candidate_id"]
        assert final["blueprint"]["structural_logic"] == target["structural_logic"]
        assert final["blueprint"]["sections"][0]["source_candidate"] == target["candidate_id"]

    def test_override_fails_closed_without_candidate_payload(self):
        charter, result = _candidates("How do mechanisms and limitations shape radiative cooling?")
        recommendation = BlueprintTournamentJudge().evaluate_and_recommend(
            candidates=result["candidates"], charter=charter
        )
        other = next(cid for cid in ("BP-A", "BP-B", "BP-C") if cid != recommendation["selected_candidate_id"])
        with pytest.raises(ValueError, match="matching candidate set"):
            BlueprintTournamentJudge.finalize(recommendation, override={"choice_id": other})


class TestF2SectionContracts:
    def test_section_identity_and_paragraph_contract_propagate(self):
        charter, result = _candidates("How do mechanisms and limitations shape radiative cooling?")
        recommendation = BlueprintTournamentJudge().evaluate_and_recommend(
            candidates=result["candidates"], charter=charter
        )
        selected = BlueprintTournamentJudge.finalize(recommendation)["blueprint"]
        output = SectionContractDesigner().design_contracts(charter=charter, blueprint=selected)
        assert output["section_count"] == len(selected["sections"])
        for section, contract in zip(selected["sections"], output["contracts"]):
            assert contract["section_id"] == section["section_id"]
            assert contract["section_title"] == section["section_title"]
            assert contract["central_thesis"] == section["planned_thesis"]["text"]
            assert contract["thesis_status"] == "planned"
            assert len(contract["paragraph_functions"]) == max(2, round(contract["word_budget"] / 250))
            assert contract["required_evidence_roles"]
            assert contract["forbidden_overclaims"]

    def test_contract_artifact_is_json_serializable(self):
        charter, result = _candidates("Compare optical cooling approaches.")
        recommendation = BlueprintTournamentJudge().evaluate_and_recommend(
            candidates=result["candidates"], charter=charter
        )
        selected = BlueprintTournamentJudge.finalize(recommendation)["blueprint"]
        output = SectionContractDesigner().design_contracts(charter=charter, blueprint=selected)
        json.dumps(output, ensure_ascii=False)


class TestF2OrchestratorIntegration:
    def test_attempt_bound_human_override_propagates_to_s9(self, tmp_path):
        orchestrator = FullReviewOrchestrator(output_dir=tmp_path, real_llm=False)
        state = FullReviewState.new(
            user_query="Compare optical cooling approaches.", domain="optical_science"
        )
        state = orchestrator.run(state)
        assert state.status == "needs_human"
        recommendation_path = state.selected_blueprint_ref.path
        recommendation = json.loads(open(recommendation_path, encoding="utf-8").read())
        other = next(
            cid for cid in ("BP-A", "BP-B", "BP-C")
            if cid != recommendation["selected_candidate_id"]
        )
        override_path = __import__("pathlib").Path(recommendation_path).parent / "blueprint_override.json"
        override_path.write_text(json.dumps({"choice_id": other}), encoding="utf-8")

        state = orchestrator.run(state)
        selected = json.loads(open(state.selected_blueprint_ref.path, encoding="utf-8").read())
        contracts = json.loads(open(state.section_contracts_ref.path, encoding="utf-8").read())
        assert selected["selected_candidate_id"] == other
        assert selected["blueprint"]["structural_logic"] == {
            "BP-A": "argument_first",
            "BP-B": "chronological_synthesis",
            "BP-C": "taxonomic_contrast",
        }[other]
        assert [c["section_id"] for c in contracts["contracts"]] == [
            s["section_id"] for s in selected["blueprint"]["sections"]
        ]


def test_s6_mentor_advice_injects_command_knowledge_and_case_moves():
    """S6 build_advice must return an operational manual, not only M1 moves."""

    advice = ReviewMentorAgent(real_llm=False).build_advice(
        user_question="How do mechanisms and limitations shape radiative cooling?",
        problem_understanding="Compare physical mechanisms.",
        scope_definition="Optical science.",
    )
    command_knowledge = advice["command_knowledge"]
    assert command_knowledge["status"] == "ok"
    assert command_knowledge["precedence"] == "process_and_judgment"
    assert command_knowledge["evidence_prohibition"] is True
    assert {
        skill["name"] for skill in command_knowledge["skills"]
    } == {
        "top-review-architecture",
        "section-review-authoring",
        "global-review-audit",
        "manuscript-integration",
    }
    assert all(skill["digest"] for skill in command_knowledge["skills"])
    assert "top-review-architecture" in command_knowledge["prompt_block"]

    assert advice["m1_case_moves"]["precedence"] == "concrete_case_moves"
    assert advice["workflow_precedence"]["order"] == [
        "command_knowledge",
        "m1_case_moves",
    ]
    assert advice["workflow_precedence"]["does_not_rank_evidence"] is True
    assert advice["evidence_authority"]["authority"] == "papers_and_material_dossiers"
    assert (
        advice["evidence_authority"]["conflict_policy"]
        == "evidence_wins_or_claim_refused"
    )
    assert "cannot resolve scientific facts" in advice[
        "command_knowledge_boundary"
    ].lower()
    assert "guidance_precedence" not in advice
    # Legacy S6 artifact consumers still see the original keys.
    assert "usable_intellectual_moves" in advice
    assert "mentor_summary" in advice
