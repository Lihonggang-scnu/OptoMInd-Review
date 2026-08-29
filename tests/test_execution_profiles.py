"""Production-entry regressions for execution-profile wiring."""

from __future__ import annotations

import run_review_harness as harness
from optomind_research.runtime.topic_scoped_kb_stage import derive_topic_scope_contract


def test_private_study_profile_reaches_cli_defaults():
    """An empty production invocation must not fall back to stale CLI literals."""
    args = harness.build_parser().parse_args(["--question", "test question"])
    profile = harness._execution_profile(args)
    assert profile == {
        "visual_fulltext_processing": True,
        "oa_fulltext_paper_cap": 6,
        "visual_max_generated_images": 4,
        "llm_style_pipeline_enabled": True,
        "chapter_style_governance_enabled": True,
        "execution_profile": "private_study",
    }


def test_library_profile_stays_offline_and_explicit_flags_win():
    args = harness.build_parser().parse_args(
        [
            "--question", "test question",
            "--execution-profile", "library_offline",
            "--visual-fulltext-processing",
            "--oa-fulltext-paper-cap", "999",
            "--visual-max-generated-images", "2",
        ]
    )
    profile = harness._execution_profile(args)
    assert profile["visual_fulltext_processing"] is True
    assert profile["oa_fulltext_paper_cap"] == 10
    assert profile["visual_max_generated_images"] == 2
    assert profile["llm_style_pipeline_enabled"] is False
    assert profile["chapter_style_governance_enabled"] is False


def test_compound_object_detection_is_plan_derived_across_domains():
    """No domain vocabulary is added: multi-word planner objects win over physics."""
    for phrase in (
        "passive daytime radiative cooling",
        "perovskite solar cell stability",
        "electrochemical carbon dioxide reduction",
    ):
        contract = derive_topic_scope_contract(
            {
                "problem_understanding": f"Review {phrase} physics",
                "main_scope": phrase,
                "keywords": [phrase],
                "scope_items": [phrase],
            }
        )
        assert contract.object_anchor_mode == "scientific_object"
        assert phrase in contract.compound_object_phrases
