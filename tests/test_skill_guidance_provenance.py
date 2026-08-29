"""Focused tests for the command-knowledge provenance/metadata contract.

Covers:
- metadata validation (required fields, types, adoption modes, attributions);
- evidence prohibition (bodies must not contain citations or concrete REF
  markers);
- skill discoverability through both the new contract loader and AgentScope's
  LocalSkillLoader.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from optomind_research.runtime.skill_guidance import (
    SkillMetadataError,
    build_skill_guidance_prompt,
    discover_command_bundles,
    load_command_bundle,
    load_command_bundle_by_name,
    load_provenance,
)
from optomind_research.runtime.skill_loader import (
    FilteredSkillLoader,
    get_command_skill_bundles,
    get_skill_guidance_prompt,
    get_skill_loader,
)
from optomind_research.review_mentor_agent import ReviewMentorAgent

SKILLS_DIR = PROJECT_ROOT / "skills"

EXPECTED_COMMAND_SKILLS = {
    "top-review-architecture",
    "section-review-authoring",
    "global-review-audit",
    "manuscript-integration",
}

_COMMAND_BODY = (
    "STATUS: command knowledge only - NOT scientific evidence.\n\n"
    "# Test manual\n\n"
    "Operational instructions for the test role. "
    "Use `[REF:paper_id]` only as instructional syntax and never as a real "
    "citation. Never invent numbers, measurements, or paper facts. "
    "Validate before completion and record gaps honestly.\n"
)


def _base_provenance(name: str) -> dict:
    return {
        "schema_version": "1.0",
        "skill": name,
        "skill_version": "1.0.0",
        "role": "planning_commands",
        "adoption_mode": "original_synthesis",
        "applicability": "Test applicability string.",
        "evidence_prohibition": True,
        "source": {
            "project": "OptoMind",
            "repository": "local-test",
            "commit": "unknown",
            "stars_observed": None,
            "stars_observed_date": None,
        },
        "influences": [
            {
                "project": "test-project",
                "repository": "unknown",
                "commit": "unknown",
                "stars_observed": "unknown",
                "stars_observed_date": "unknown",
                "license": "MIT",
                "adoption_mode": "compatible_adaptation",
                "notes": "test influence",
            }
        ],
    }


def _write_skill(
    root: Path,
    name: str,
    *,
    body: str = _COMMAND_BODY,
    provenance: dict | None = None,
) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\n{body}",
        encoding="utf-8",
    )
    (skill_dir / "provenance.json").write_text(
        json.dumps(
            provenance if provenance is not None else _base_provenance(name),
            indent=2,
        ),
        encoding="utf-8",
    )
    return skill_dir


def test_expected_command_skills_are_discoverable_and_valid():
    bundles = discover_command_bundles(SKILLS_DIR)
    names = {bundle.name for bundle in bundles}
    assert EXPECTED_COMMAND_SKILLS <= names, (
        f"missing command skills: {sorted(EXPECTED_COMMAND_SKILLS - names)}"
    )
    for bundle in bundles:
        if bundle.name not in EXPECTED_COMMAND_SKILLS:
            continue
        assert bundle.evidence_prohibition is True
        assert bundle.version.count(".") == 2
        assert len(bundle.instructions) > 500
        assert "not scientific evidence" in bundle.instructions.lower()


def test_provenance_metadata_records_required_attributions():
    for name in EXPECTED_COMMAND_SKILLS:
        provenance = load_provenance(SKILLS_DIR / name)
        assert provenance.skill == name
        assert provenance.source["commit"]
        influences = {
            str(item["project"]): item for item in provenance.influences
        }
        assert influences["academic-research-skills"]["license"] == "CC BY-NC"
        assert (
            influences["academic-research-skills"]["adoption_mode"]
            == "idea_only"
        )
        assert (
            "no text copied"
            in influences["academic-research-skills"]["notes"].lower()
        )
        assert influences["Orchestra"]["license"] == "MIT"
        assert influences["STORM (Stanford OVAL)"]["license"] == "MIT"
        assert influences["AutoSurvey"]["license"] == "unknown"
        assert (
            influences["K-Dense scientific-agent-skills"]["license"]
            == "unknown"
        )
        assert influences["OpenScholar"]["license"] == "unknown"
        assert influences["GPT Researcher"]["license"] == "MIT"
        assert (
            influences["LangChain (Open Deep Research)"]["license"] == "MIT"
        )
        assert influences["OpenAI Cookbook (Deep Research)"]["license"] == "MIT"
        assert influences["PaperQA / PaperQA2"]["stars_observed"]
        # Unknown fields must say unknown instead of being fabricated.
        for item in provenance.influences:
            assert str(item["repository"]).strip(), (
                f"{item['project']} missing repository"
            )
            assert str(item["commit"]).strip(), (
                f"{item['project']} missing commit"
            )
        assert influences["AutoSurvey"]["repository"] == "unknown"
        assert influences["AutoSurvey"]["commit"] == "unknown"
        assert (
            influences["K-Dense scientific-agent-skills"]["repository"]
            == "unknown"
        )
        assert influences["OpenScholar"]["commit"] == "unknown"


def test_missing_required_metadata_field_rejected(tmp_path):
    provenance = _base_provenance("bad-skill")
    del provenance["evidence_prohibition"]
    _write_skill(tmp_path, "bad-skill", provenance=provenance)
    with pytest.raises(SkillMetadataError, match="evidence_prohibition"):
        load_command_bundle(tmp_path / "bad-skill")


def test_evidence_prohibition_false_rejected(tmp_path):
    provenance = _base_provenance("bad-skill")
    provenance["evidence_prohibition"] = False
    _write_skill(tmp_path, "bad-skill", provenance=provenance)
    with pytest.raises(SkillMetadataError, match="evidence_prohibition"):
        load_command_bundle(tmp_path / "bad-skill")


def test_invalid_adoption_mode_rejected(tmp_path):
    provenance = _base_provenance("bad-skill")
    provenance["adoption_mode"] = "unknown-mode"
    _write_skill(tmp_path, "bad-skill", provenance=provenance)
    with pytest.raises(SkillMetadataError, match="adoption_mode"):
        load_command_bundle(tmp_path / "bad-skill")


def test_invalid_schema_version_rejected(tmp_path):
    provenance = _base_provenance("bad-skill")
    provenance["schema_version"] = "0.9"
    _write_skill(tmp_path, "bad-skill", provenance=provenance)
    with pytest.raises(SkillMetadataError, match="schema_version"):
        load_command_bundle(tmp_path / "bad-skill")


def test_instruction_body_with_url_citation_rejected(tmp_path):
    _write_skill(
        tmp_path,
        "bad-skill",
        body=_COMMAND_BODY
        + "\nSee https://doi.org/10.1000/example for details.\n",
    )
    with pytest.raises(SkillMetadataError, match="citation"):
        load_command_bundle(tmp_path / "bad-skill")


def test_instruction_body_with_concrete_ref_marker_rejected(tmp_path):
    _write_skill(
        tmp_path,
        "bad-skill",
        body=_COMMAND_BODY + "\nUse [REF:paper_001] here.\n",
    )
    with pytest.raises(SkillMetadataError, match="REF marker"):
        load_command_bundle(tmp_path / "bad-skill")


def test_placeholder_ref_markers_are_allowed(tmp_path):
    body = (
        "STATUS: command knowledge only - NOT scientific evidence.\n\n"
        "Write `[REF:paper_id]` per sentence and never use `[REF:UNKNOWN]` "
        "as a placeholder. For structural transitions use `[REF:*]`. "
        "Never invent numbers, measurements, or paper facts. "
        "Validate before completion and record gaps honestly.\n"
    )
    _write_skill(tmp_path, "ok-skill", body=body)
    bundle = load_command_bundle(tmp_path / "ok-skill")
    assert bundle.name == "ok-skill"
    assert bundle.evidence_prohibition is True


def test_prompt_block_excludes_provenance_details():
    bundle = load_command_bundle_by_name(
        "top-review-architecture",
        skills_dir=SKILLS_DIR,
    )
    block = bundle.to_prompt_block()
    assert "[SKILL:top-review-architecture" in block
    assert "command knowledge" in block
    assert "not scientific evidence" in block
    assert "CC BY-NC" not in block
    assert '"influences"' not in block
    assert "academic-research-skills" not in block


def test_discover_strict_raises_and_loose_skips(tmp_path):
    _write_skill(tmp_path, "good-skill")
    bad = _base_provenance("bad-skill")
    del bad["schema_version"]
    _write_skill(tmp_path, "bad-skill", provenance=bad)
    with pytest.raises(SkillMetadataError):
        discover_command_bundles(tmp_path, strict=True)
    bundles = discover_command_bundles(tmp_path, strict=False)
    assert [bundle.name for bundle in bundles] == ["good-skill"]


def test_agentscope_loader_and_skill_loader_helpers():
    loader = FilteredSkillLoader(
        get_skill_loader(SKILLS_DIR),
        skill_ids=["*"],
    )
    names = {skill.name for skill in asyncio.run(loader.list_skills())}
    assert EXPECTED_COMMAND_SKILLS <= names, (
        f"missing from LocalSkillLoader: {sorted(EXPECTED_COMMAND_SKILLS - names)}"
    )

    bundles = get_command_skill_bundles(
        SKILLS_DIR,
        skill_ids=["top-review-architecture"],
    )
    assert [bundle.name for bundle in bundles] == [
        "top-review-architecture"
    ]
    assert get_command_skill_bundles(SKILLS_DIR, skill_ids=[]) == []
    assert {
        bundle.name
        for bundle in get_command_skill_bundles(SKILLS_DIR, skill_ids=["*"])
    } == EXPECTED_COMMAND_SKILLS

    prompt = get_skill_guidance_prompt(
        SKILLS_DIR,
        skill_ids=["global-review-audit"],
    )
    assert "global-review-audit" in prompt
    assert "top-review-architecture" not in prompt

    all_prompt = build_skill_guidance_prompt(SKILLS_DIR)
    for name in EXPECTED_COMMAND_SKILLS:
        assert f"[SKILL:{name}" in all_prompt


def _mentor_advice(**kwargs) -> dict:
    agent = ReviewMentorAgent(real_llm=False, **kwargs)
    return agent.build_advice(
        user_question="How do mechanisms shape radiative cooling?",
        problem_understanding="Compare physical mechanisms.",
        scope_definition="Optical science.",
    )


def test_review_mentor_build_advice_injects_command_knowledge():
    advice = _mentor_advice()
    assert "command_knowledge" in advice
    assert "m1_case_moves" in advice

    ck = advice["command_knowledge"]
    assert ck["status"] == "ok"
    assert ck["precedence"] == "process_and_judgment"
    assert ck["evidence_prohibition"] is True
    assert {skill["name"] for skill in ck["skills"]} == EXPECTED_COMMAND_SKILLS
    for skill in ck["skills"]:
        assert skill["version"].count(".") == 2
        assert len(skill["digest"]) == 64
        assert skill["provenance"]["source"]["commit"]
        assert skill["provenance"]["adoption_mode"]
        assert "command knowledge" in skill["instructions"].lower()
    assert "top-review-architecture" in ck["prompt_block"]
    assert "guidance_precedence" not in advice

    m1 = advice["m1_case_moves"]
    assert m1["precedence"] == "concrete_case_moves"
    assert m1["evidence_prohibition"] is True
    assert m1["selected_move_counts"] == advice["selected_move_counts"]

    # Legacy consumers keep every pre-existing key.
    for key in (
        "mentor_summary",
        "usable_intellectual_moves",
        "m2a_claim_decomposition_advice",
        "m2b_argument_dag_advice",
        "m3_gap_resolution_advice",
        "visual_argument_advice",
        "quality_risks",
        "selected_moves_digest",
    ):
        assert key in advice


def test_review_mentor_precedence_and_evidence_prohibition():
    advice = _mentor_advice()

    # Contract 1: workflow/judgment precedence ranks only the two guidance layers.
    workflow = advice["workflow_precedence"]
    assert workflow["contract"] == "workflow_and_judgment_precedence"
    assert workflow["scope"] == "guidance_layers_only"
    assert workflow["order"] == ["command_knowledge", "m1_case_moves"]
    assert "scientific_evidence" not in workflow["order"]
    assert workflow["does_not_rank_evidence"] is True
    assert "process and judgment precedence" in workflow["command_knowledge"].lower()
    assert "never override" in workflow["m1_case_moves"].lower()

    # Contract 2: evidence authority is exclusive and orthogonal.
    authority = advice["evidence_authority"]
    assert authority["contract"] == "scientific_evidence_authority"
    assert authority["authority"] == "papers_and_material_dossiers"
    assert authority["exclusive"] is True
    assert authority["conflict_policy"] == "evidence_wins_or_claim_refused"
    assert set(authority["cannot_resolve"]) == {
        "facts",
        "measurements",
        "mechanisms",
        "citations",
    }
    assert "evidence wins" in authority["statement"].lower()
    assert "claim is refused" in authority["statement"].lower()

    # Prompt-facing boundary: command knowledge cannot resolve science.
    boundary = advice["command_knowledge_boundary"].lower()
    assert "cannot resolve scientific facts" in boundary
    assert "measurements" in boundary
    assert "mechanisms" in boundary
    assert "citations" in boundary
    assert "evidence wins or the claim is refused" in boundary

    assert advice["command_knowledge"]["evidence_prohibition"] is True
    assert advice["m1_case_moves"]["evidence_prohibition"] is True
    assert "guidance_precedence" not in advice


def test_review_mentor_missing_skills_diagnosed_not_silent(tmp_path):
    empty = tmp_path / "empty-skills"
    empty.mkdir()
    advice = _mentor_advice(skills_dir=empty)
    ck = advice["command_knowledge"]
    assert ck["status"] == "error"
    assert ck["diagnosis"]
    assert ck["fallback_used"] is False
    assert ck["skills"] == []
    # M1 case moves still exist, but the failure is explicit, never silent.
    assert advice["m1_case_moves"]["precedence"] == "concrete_case_moves"


def test_review_mentor_missing_skills_fail_closed_for_production(tmp_path):
    empty = tmp_path / "empty-skills"
    empty.mkdir()
    agent = ReviewMentorAgent(real_llm=True, skills_dir=empty)
    with pytest.raises(SkillMetadataError, match="command-knowledge"):
        agent.build_advice(user_question="Question")


def test_review_mentor_malformed_provenance_fail_closed(tmp_path):
    skill_dir = tmp_path / "top-review-architecture"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: top-review-architecture\ndescription: x\n---\n\n"
        "# Manual\n\n" + ("operational instructions " * 80),
        encoding="utf-8",
    )
    (skill_dir / "provenance.json").write_text("{}", encoding="utf-8")
    agent = ReviewMentorAgent(real_llm=True, skills_dir=tmp_path)
    with pytest.raises(SkillMetadataError, match="command-knowledge"):
        agent.build_advice(user_question="Question")


def test_review_mentor_command_skill_filtering():
    agent = ReviewMentorAgent(
        real_llm=False,
        command_skill_ids=("top-review-architecture",),
    )
    advice = agent.build_advice(user_question="Question")
    assert [skill["name"] for skill in advice["command_knowledge"]["skills"]] == [
        "top-review-architecture"
    ]
