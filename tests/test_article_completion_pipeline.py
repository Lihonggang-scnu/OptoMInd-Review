"""Acceptance tests for article-level review completion contracts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agentscope.tool import FunctionTool
from pydantic import ValidationError

from optomind_research.runtime.article_completion_schemas import (
    ArticleRhetoricalContract,
)
from optomind_research.runtime.article_synthesis_map_builder import (
    collect_article_synthesis_inputs,
    sanitize_article_synthesis_map,
)
from optomind_research.runtime.article_completion_tool_provider import (
    ArticleCompletionContext,
    ArticleCompletionToolProvider,
    _coerce_string_list_fields,
    _unsupported_numeric_sentences,
)
from optomind_research.runtime.article_structure_auditor import (
    audit_complete_manuscript,
)
from optomind_research.runtime.complete_manuscript_assembler import (
    assemble_complete_manuscript,
)
from optomind_research.runtime.global_figure_planner import (
    build_global_figure_plan,
    merge_global_figures_into_visual_plan,
)
from optomind_research.runtime.latex_publication_renderer import (
    _strip_embedded_title_and_abstract,
    resolve_publication_metadata,
)
from optomind_research.runtime.review_lead_tool_provider import (
    ReviewLeadContext,
    ReviewLeadToolProvider,
)
from optomind_research.runtime.section_authoring_tool_registry import (
    SectionAuthoringToolProvider,
)
from optomind_research.runtime.tool_provider import SectionAuthoringContext
from tests.test_research_harness_upgrade import (
    _sample_blueprint,
    _test_topic_identity,
)


def _tool_text(tool: FunctionTool, **kwargs) -> str:
    value = tool(**kwargs)
    result = asyncio.run(value) if asyncio.iscoroutine(value) else value
    return " ".join(
        block.text for block in result.content if hasattr(block, "text")
    )


def _native_contract(blueprint: dict) -> dict:
    return {
        "schema_version": "article_rhetorical_contract.v1",
        "methodology_identity": "critical_narrative_review",
        "target_article_type": "comprehensive_review",
        "target_audience": [
            "optics researchers",
            "graduate students",
            "adjacent-domain specialists",
        ],
        "provisional_title": "A mechanism-centred review of a physical topic",
        "central_question": (
            "How should the confirmed physical topic be interpreted across "
            "mechanisms, methods, and application boundaries?"
        ),
        "review_thesis": blueprint["review_thesis"],
        "distinctive_angle": blueprint["full_review_argument"],
        "scope_inclusions": [
            "Governing optical mechanisms and their practical limits."
        ],
        "scope_exclusions": [
            "Unrelated optimisation methods without a physical connection."
        ],
        "introduction_contract": {
            "why_now": (
                "Recent technical diversification makes a mechanism-centred "
                "synthesis necessary."
            ),
            "reader_prerequisites": [
                "Define only the optical concepts needed for the argument."
            ],
            "problem_reframing": blueprint["review_thesis"],
            "review_gap": (
                "Existing accounts do not consistently connect mechanisms, "
                "evidence quality, and deployment boundaries."
            ),
            "scope_and_method_disclosure": (
                "Declare a critical narrative review and the confirmed scope."
            ),
            "roadmap_function": blueprint["full_review_argument"],
        },
        "body_contract": {
            "primary_taxonomy": blueprint["taxonomy_principle"],
            "cross_cutting_dimensions": [
                "evidence quality",
                "manufacturability",
                "application fit",
            ],
            "progression_logic": blueprint["narrative_strategy"],
            "must_resolve": [
                section["chapter_argument"]
                for section in blueprint["sections"]
            ],
        },
        "outlook_contract": {
            "challenge_axes": [
                "physical_limit",
                "measurement_or_benchmark_gap",
                "application_or_translation",
            ],
            "required_reasoning": [
                "state_of_evidence",
                "root_cause",
                "current_response",
                "remaining_boundary",
                "actionable_next_step",
            ],
            "speculation_policy": (
                "Separate established, conditional, and speculative statements."
            ),
        },
        "conclusion_contract": {
            "required_takeaways": 4,
            "must_answer_central_question": True,
            "no_new_topics": True,
            "no_new_evidence": True,
        },
        "abstract_contract": {
            "target_word_range": {"min": 180, "max": 300},
            "required_moves": [
                "context",
                "need",
                "scope",
                "organizing_logic",
                "major_synthesis",
                "outlook",
            ],
        },
        "global_figure_contract": {
            "candidate_templates": [
                "field_map",
                "timeline",
                "benchmark_landscape",
                "challenge_roadmap",
            ],
            "selection_rule": (
                "Use only figures with material explanatory or argumentative value."
            ),
        },
        "provenance": "native",
    }


def _provider(tmp_path: Path) -> ReviewLeadToolProvider:
    return ReviewLeadToolProvider(
        ReviewLeadContext(
            user_question="Review a general optical research topic.",
            problem_understanding=(
                "Review mechanisms, methods, frontiers, and applications."
            ),
            scope_definition=(
                "Cover governing physics, evidence quality, and practical limits."
            ),
            work_dir=tmp_path,
            topic_identity=_test_topic_identity(),
        )
    )


def test_native_article_rhetorical_contract_validates(tmp_path: Path):
    blueprint = _sample_blueprint()
    blueprint["article_rhetorical_contract"] = _native_contract(blueprint)
    provider = _provider(tmp_path)
    tools = {tool.name: tool for tool in provider.get_tools(tmp_path)}

    submitted = json.loads(
        _tool_text(
            tools["submit_review_blueprint"],
            blueprint_json=json.dumps(blueprint),
        )
    )
    assert submitted["rhetorical_contract_provenance"] == "native"
    assert "VALIDATION_PASSED" in _tool_text(
        tools["validate_review_blueprint_package"]
    )
    report = json.loads(provider.validation_path.read_text(encoding="utf-8"))
    assert report["article_rhetorical_contract_status"] == "valid"
    assert report["article_rhetorical_contract_provenance"] == "native"


def test_legacy_blueprint_gets_explicit_compatibility_contract(tmp_path: Path):
    provider = _provider(tmp_path)
    tools = {tool.name: tool for tool in provider.get_tools(tmp_path)}
    _tool_text(
        tools["submit_review_blueprint"],
        blueprint_json=json.dumps(_sample_blueprint()),
    )
    stored = json.loads(provider.blueprint_path.read_text(encoding="utf-8"))
    contract = ArticleRhetoricalContract.model_validate(
        stored["article_rhetorical_contract"]
    )
    assert contract.provenance == "legacy_compatibility"
    assert "VALIDATION_PASSED" in _tool_text(
        tools["validate_review_blueprint_package"]
    )


def test_contract_rejects_unknown_methodology():
    blueprint = _sample_blueprint()
    contract = _native_contract(blueprint)
    contract["methodology_identity"] = "marketing_overview"
    try:
        ArticleRhetoricalContract.model_validate(contract)
    except ValidationError:
        return
    raise AssertionError("invalid methodology must be rejected")


def test_synthesis_map_scalar_string_lists_are_repaired_losslessly():
    raw = {
        "review_wide_consensus": "one consensus",
        "section_contributions": [
            {
                "established_takeaways": "one takeaway",
                "conditional_judgments": "one conditional judgement",
            }
        ],
        "challenge_candidates": [
            {"current_responses": "one current response"}
        ],
        "outlook_candidates": [
            {
                "actionable_milestones": "one milestone",
                "success_indicators": "one indicator",
            }
        ],
    }
    repaired = _coerce_string_list_fields(raw)
    assert repaired["review_wide_consensus"] == ["one consensus"]
    assert repaired["section_contributions"][0]["established_takeaways"] == [
        "one takeaway"
    ]
    assert repaired["challenge_candidates"][0]["current_responses"] == [
        "one current response"
    ]
    assert repaired["outlook_candidates"][0]["actionable_milestones"] == [
        "one milestone"
    ]


def test_precise_numeric_claims_require_reference_or_proposal_label():
    assert _unsupported_numeric_sentences(
        "Reported coupling efficiency remains below 50% in most devices."
    )
    assert not _unsupported_numeric_sentences(
        "Reported coupling efficiency is 48% [REF:paper-real]."
    )
    assert not _unsupported_numeric_sentences(
        "A proposed target is 80% coupling efficiency."
    )


def test_body_sections_remain_separate_from_front_and_back_matter():
    blueprint = _sample_blueprint()
    blueprint["article_rhetorical_contract"] = _native_contract(blueprint)
    titles = [section["title"].lower() for section in blueprint["sections"]]
    assert not any(
        title in {"introduction", "abstract", "conclusion", "outlook"}
        for title in titles
    )


def test_handoff_card_recomputes_provenance_ids(tmp_path: Path):
    section_dir = tmp_path / "sections" / "S01"
    section_dir.mkdir(parents=True)
    (section_dir / "SECTION_CITATION_MAP.json").write_text(
        json.dumps(
            {
                "citations": [
                    {
                        "paper_ids": ["paper-real"],
                        "chunk_ids": ["chunk-real"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    image_path = section_dir / "figure.png"
    image_path.write_bytes(b"test")
    (section_dir / "SECTION_VISUAL_PLACEMENT.json").write_text(
        json.dumps(
            {
                "placements": [
                    {
                        "visual_chunk_id": "visual-real",
                        "asset_status": "verified_local",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    ctx = SectionAuthoringContext(
        section_id="S01",
        section_data={
            "title": "Physical mechanism",
            "chapter_argument": "Explain the governing mechanism.",
        },
        kb_sqlite=None,
        temp_kb_sqlite=None,
        work_dir=section_dir,
    )
    tools = {
        tool.name: tool
        for tool in SectionAuthoringToolProvider(ctx).get_tools(section_dir)
    }
    result = json.loads(
        _tool_text(
            tools["submit_section_handoff_card"],
            handoff_json=json.dumps(
                {
                    "section_id": "fabricated-section",
                    "section_argument_completed": True,
                    "established_takeaways": [
                        "The section establishes a bounded physical conclusion."
                    ],
                    "conditional_judgments": [
                        "The conclusion remains conditional on material loss."
                    ],
                    "unresolved_tensions": [
                        "The deployment boundary remains unresolved."
                    ],
                    "terms_defined": ["governing mechanism"],
                    "avoid_repeating": ["the general field definition"],
                    "forward_question": "How can the limit be engineered?",
                    "why_next_section_is_needed": (
                        "The next section compares available design routes."
                    ),
                    "visual_takeaways": [
                        {
                            "visual_chunk_id": "visual-real",
                            "argumentative_function": "Clarifies the mechanism.",
                        },
                        {
                            "visual_chunk_id": "visual-fabricated",
                            "argumentative_function": "Must be removed.",
                        },
                    ],
                    "used_paper_ids": ["paper-fabricated"],
                    "used_chunk_ids": ["chunk-fabricated"],
                }
            ),
        )
    )
    assert result["status"] == "ok"
    card = json.loads(
        (section_dir / "SECTION_HANDOFF_CARD.json").read_text(
            encoding="utf-8"
        )
    )
    assert card["section_id"] == "S01"
    assert card["used_paper_ids"] == ["paper-real"]
    assert card["used_chunk_ids"] == ["chunk-real"]
    assert [
        item["visual_chunk_id"] for item in card["visual_takeaways"]
    ] == ["visual-real"]


def test_synthesis_input_and_map_filter_unverified_ids(tmp_path: Path):
    blueprint = _sample_blueprint()
    blueprint["article_rhetorical_contract"] = _native_contract(blueprint)
    blueprint["input_context"] = {"user_question": "A physical review question"}
    blueprint_path = tmp_path / "REVIEW_BLUEPRINT.json"
    blueprint_path.write_text(json.dumps(blueprint), encoding="utf-8")
    sections_root = tmp_path / "sections"
    for section in blueprint["sections"]:
        section_dir = sections_root / section["section_id"]
        section_dir.mkdir(parents=True)
        paper_id = "paper-" + section["section_id"]
        chunk_id = "chunk-" + section["section_id"]
        (section_dir / "SECTION_DRAFT_EN.md").write_text(
            (
                "This section develops a distinct physical argument. "
                "Its closing judgement establishes a bounded conclusion."
            ),
            encoding="utf-8",
        )
        (section_dir / "SECTION_CITATION_MAP.json").write_text(
            json.dumps(
                {
                    "citations": [
                        {
                            "paper_ids": [paper_id],
                            "chunk_ids": [chunk_id],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
    synthesis_input = collect_article_synthesis_inputs(
        blueprint_path,
        sections_root,
    )
    raw = {
        "section_contributions": [
            {
                "section_id": section["section_id"],
                "argument_role": section["argument_role"],
                "established_takeaways": [
                    "A bounded section-level conclusion."
                ],
            }
            for section in blueprint["sections"]
        ]
        + [{"section_id": "S99", "argument_role": "fabricated"}],
        "review_wide_consensus": ["A cross-section consensus."],
        "review_wide_disagreements": ["A bounded disagreement."],
        "cross_section_tradeoffs": ["A physical trade-off."],
        "challenge_candidates": [
            {
                "challenge_id": "CH01",
                "statement": "A linked scientific challenge remains.",
                "linked_section_ids": ["S01", "S99"],
                "evidence_state": "conditional",
                "root_cause": "Competing physical requirements.",
                "current_responses": ["A partial response."],
                "remaining_boundary": "The general boundary is unresolved.",
            }
        ],
        "outlook_candidates": [
            {
                "opportunity_id": "OP01",
                "linked_challenge_ids": ["CH01", "CH99"],
                "direction": "Test a bounded route.",
                "actionable_milestones": ["Define a benchmark."],
                "success_indicators": ["Reproducible improvement."],
                "confidence": "medium",
                "downstream_research_plan_ready": True,
            }
        ],
        "conclusion_candidates": ["A final bounded takeaway."],
        "intro_promise_candidates": ["Explain the shared constraint."],
        "reference_inventory": {
            "unique_paper_ids": ["paper-fabricated"],
            "landmark_paper_ids": ["paper-S01", "paper-fabricated"],
            "frontier_paper_ids": [],
        },
        "visual_inventory": {
            "existing_visual_ids": ["visual-fabricated"],
            "global_figure_opportunities": ["field map"],
        },
    }
    result, audit = sanitize_article_synthesis_map(raw, synthesis_input)
    assert len(result.section_contributions) == 5
    assert result.challenge_candidates[0].linked_section_ids == ["S01"]
    assert result.outlook_candidates[0].linked_challenge_ids == ["CH01"]
    assert "paper-fabricated" not in result.reference_inventory.unique_paper_ids
    assert "S99" in audit["removed_unverified_ids"]["section_ids"]
    assert "CH99" in audit["removed_unverified_ids"]["challenge_ids"]


def _make_completion_fixture(tmp_path: Path) -> tuple[Path, Path, dict]:
    blueprint = _sample_blueprint()
    blueprint["article_rhetorical_contract"] = _native_contract(blueprint)
    blueprint["input_context"] = {
        "user_question": "How should a physical topic be synthesized?"
    }
    blueprint_path = tmp_path / "REVIEW_BLUEPRINT.json"
    blueprint_path.write_text(json.dumps(blueprint), encoding="utf-8")
    sections_root = tmp_path / "sections"
    for section in blueprint["sections"]:
        section_dir = sections_root / section["section_id"]
        section_dir.mkdir(parents=True)
        paper_id = "paper-" + section["section_id"]
        chunk_id = "chunk-" + section["section_id"]
        (section_dir / "SECTION_DRAFT_EN.md").write_text(
            (
                "This section develops its assigned physical argument from "
                f"audited evidence [REF:{paper_id}]. The final judgement "
                "defines a bounded implication for the next section."
            ),
            encoding="utf-8",
        )
        (section_dir / "SECTION_CITATION_MAP.json").write_text(
            json.dumps(
                {
                    "citations": [
                        {
                            "paper_ids": [paper_id],
                            "chunk_ids": [chunk_id],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
    return blueprint_path, sections_root, blueprint


def _raw_synthesis_map(blueprint: dict) -> dict:
    return {
        "section_contributions": [
            {
                "section_id": section["section_id"],
                "argument_role": section["argument_role"],
                "established_takeaways": [
                    "The section establishes its bounded physical conclusion."
                ],
                "conditional_judgments": [
                    "Its transfer depends on the stated operating conditions."
                ],
                "unresolved_tensions": [
                    "One cross-platform boundary remains unresolved."
                ],
            }
            for section in blueprint["sections"]
        ],
        "review_wide_consensus": [
            "The body supports a shared mechanism-centred interpretation."
        ],
        "review_wide_disagreements": [
            "Evidence remains divided at one deployment boundary."
        ],
        "cross_section_tradeoffs": [
            "Improving one optical response can constrain another requirement."
        ],
        "challenge_candidates": [
            {
                "challenge_id": "CH01",
                "statement": "A cross-section trade-off remains.",
                "linked_section_ids": ["S02", "S04"],
                "evidence_state": "conditional",
                "root_cause": "Competing physical requirements.",
                "current_responses": ["Separate control channels."],
                "remaining_boundary": "Generalization remains uncertain.",
            }
        ],
        "outlook_candidates": [
            {
                "opportunity_id": "OP01",
                "linked_challenge_ids": ["CH01"],
                "direction": "Establish a shared benchmark.",
                "actionable_milestones": ["Define comparable test conditions."],
                "success_indicators": ["Independent reproducibility."],
                "confidence": "medium",
                "downstream_research_plan_ready": True,
            }
        ],
        "conclusion_candidates": ["The shared constraint organizes the field."],
        "intro_promise_candidates": ["Connect mechanisms to practical limits."],
        "reference_inventory": {
            "unique_paper_ids": [
                "paper-" + section["section_id"]
                for section in blueprint["sections"]
            ],
            "landmark_paper_ids": ["paper-S01"],
            "frontier_paper_ids": ["paper-S04", "paper-S05"],
        },
        "visual_inventory": {
            "existing_visual_ids": [],
            "global_figure_opportunities": ["field map"],
        },
    }


def _words(sentence: str, minimum: int) -> str:
    values = []
    while len(" ".join(values).split()) < minimum:
        values.append(sentence)
    return " ".join(values)


def test_article_completion_provider_rejects_unknown_refs_and_accepts_package(
    tmp_path: Path,
):
    blueprint_path, sections_root, blueprint = _make_completion_fixture(
        tmp_path
    )
    context = ArticleCompletionContext(
        blueprint_path=blueprint_path,
        sections_root=sections_root,
        work_dir=tmp_path / "completion",
        min_introduction_words=120,
        min_outlook_words=120,
        min_conclusion_words=80,
    )
    provider = ArticleCompletionToolProvider(context)
    tools = {
        tool.name: tool
        for tool in provider.get_tools(context.work_dir)
    }
    loaded = json.loads(_tool_text(tools["load_article_completion_context"]))
    assert loaded["status"] == "ok"
    submitted_map = json.loads(
        _tool_text(
            tools["submit_article_synthesis_map"],
            synthesis_map_json=json.dumps(_raw_synthesis_map(blueprint)),
        )
    )
    assert submitted_map["status"] == "ok"

    package = {
        "schema_version": "article_completion_package.v1",
        "title": "A mechanism-centred synthesis of a physical topic",
        "abstract": _words(
            "This review connects mechanisms evidence boundaries and actionable research priorities.",
            185,
        ),
        "introduction": "This critical narrative review defines the method. " + _words(
            "The field now requires a bounded synthesis that connects physical mechanisms to evidence quality and practical limits.",
            130,
        ),
        "challenge_and_outlook": _words(
            "The central challenge has a defined evidence state root cause current response remaining boundary and measurable next step.",
            130,
        ),
        "conclusion": _words(
            "The review answers the central question through a shared physical constraint and bounded application judgement.",
            85,
        ),
        "methodology_identity": "critical_narrative_review",
        "outlook_items": [
            {
                "opportunity_id": "OP01",
                "text_span": "Establish a shared benchmark.",
                "linked_section_ids": ["S02", "S04"],
                "linked_challenge_ids": ["CH01"],
                "evidence_state": "conditional",
                "downstream_research_plan_ready": True,
            }
        ],
        "quality_self_check": {
            "introduction_promises": [
                "Develop the physical argument.",
                "Establish a bounded conclusion.",
            ],
            "conclusion_takeaways": [
                "A shared constraint organizes the field.",
                "Evidence quality changes the strength of the judgement.",
                "Application boundaries remain conditional.",
                "A shared benchmark is the next priority.",
            ],
            "abstract_major_messages": [
                "Mechanisms and boundaries must be synthesized together.",
                "The review identifies an actionable benchmark.",
            ],
            "new_topic_declared": False,
        },
    }
    invalid = dict(package)
    invalid["introduction"] = (
        package["introduction"] + " [REF:paper-fabricated]"
    )
    rejected = json.loads(
        _tool_text(
            tools["submit_article_completion_package"],
            completion_package_json=json.dumps(invalid),
        )
    )
    assert rejected["status"] == "error"
    assert "unverified reference IDs" in " ".join(rejected["errors"])

    accepted = json.loads(
        _tool_text(
            tools["submit_article_completion_package"],
            completion_package_json=json.dumps(package),
        )
    )
    assert accepted["status"] == "ok"
    assert "VALIDATION_PASSED" in _tool_text(
        tools["validate_article_completion_package"]
    )


def test_assemble_audit_and_global_figure_routing(tmp_path: Path):
    blueprint_path, sections_root, blueprint = _make_completion_fixture(
        tmp_path
    )
    completion_dir = tmp_path / "completion"
    context = ArticleCompletionContext(
        blueprint_path=blueprint_path,
        sections_root=sections_root,
        work_dir=completion_dir,
        min_introduction_words=20,
        min_outlook_words=20,
        min_conclusion_words=20,
    )
    provider = ArticleCompletionToolProvider(context)
    tools = {
        tool.name: tool
        for tool in provider.get_tools(completion_dir)
    }
    _tool_text(tools["load_article_completion_context"])
    _tool_text(
        tools["submit_article_synthesis_map"],
        synthesis_map_json=json.dumps(_raw_synthesis_map(blueprint)),
    )
    package = {
        "schema_version": "article_completion_package.v1",
        "title": "A mechanism-centred synthesis of a physical topic",
        "abstract": _words(
            "This review connects mechanisms evidence boundaries and actionable research priorities.",
            185,
        ),
        "introduction": (
            "This critical narrative review explains why the physical topic "
            "requires renewed synthesis and defines the scope, taxonomy, and "
            "argument delivered by the body."
        ),
        "challenge_and_outlook": (
            "A conditional cross-section challenge arises from competing "
            "requirements. Its root cause, current response, remaining "
            "boundary, next benchmark, and success indicator are explicit."
        ),
        "conclusion": (
            "The central question is answered by a shared physical constraint. "
            "The review establishes a bounded mechanism-centred interpretation "
            "and identifies a measurable research priority."
        ),
        "methodology_identity": "critical_narrative_review",
        "outlook_items": [
            {
                "opportunity_id": "OP01",
                "text_span": "Establish a shared benchmark.",
                "linked_section_ids": ["S02", "S04"],
                "linked_challenge_ids": ["CH01"],
                "evidence_state": "conditional",
                "downstream_research_plan_ready": True,
            }
        ],
        "quality_self_check": {
            "introduction_promises": [
                "mechanism physical argument",
                "bounded conclusion",
            ],
            "conclusion_takeaways": [
                "A shared constraint organizes the field.",
                "Evidence quality controls confidence.",
                "Application boundaries remain conditional.",
                "A benchmark is the next priority.",
            ],
            "abstract_major_messages": [
                "Mechanisms connect to boundaries.",
                "The review identifies an actionable benchmark.",
            ],
            "new_topic_declared": False,
        },
    }
    # The test targets assembly and routing, so use permissive component minima.
    accepted = json.loads(
        _tool_text(
            tools["submit_article_completion_package"],
            completion_package_json=json.dumps(package),
        )
    )
    assert accepted["status"] == "ok"

    body_path = tmp_path / "FINAL_REVIEW_EN.md"
    body_path.write_text(
        "\n\n".join(
            f"## {section['title']}\n\n"
            + (
                f"This {section['section_id']} body section develops the mechanism physical argument "
                "from audited literature and passes a bounded conclusion "
                "forward. "
            )
            * 3
            for section in blueprint["sections"]
        ),
        encoding="utf-8",
    )
    manuscript_dir = tmp_path / "manuscript"
    manifest = assemble_complete_manuscript(
        completion_package_path=provider.package_path,
        body_review_path=body_path,
        output_dir=manuscript_dir,
    )
    report = audit_complete_manuscript(
        manuscript_path=Path(manifest["manuscript_path"]),
        body_review_path=body_path,
        completion_package_path=provider.package_path,
        blueprint_path=blueprint_path,
        output_path=manuscript_dir / "ARTICLE_STRUCTURE_AUDIT.json",
    )
    assert report["status"] in {"passed", "needs_attention"}
    assert not report["blocking_flags"]

    global_plan_path = tmp_path / "GLOBAL_FIGURE_PLAN.json"
    global_plan = build_global_figure_plan(
        blueprint_path=blueprint_path,
        synthesis_map_path=provider.map_path,
        output_path=global_plan_path,
    )
    assert any(
        item.template_kind == "field_map"
        and item.eligibility_status == "eligible"
        for item in global_plan.article_level_figures
    )
    visual_plan_path = tmp_path / "VISUAL_EDITORIAL_PLAN.json"
    visual_plan_path.write_text(
        json.dumps(
            {
                "schema_version": "research_harness.visual_editorial_plan.v1",
                "input_fingerprint": "test",
                "placements": [],
                "conceptual_figure_requests": [],
                "unfilled_visual_needs": [],
            }
        ),
        encoding="utf-8",
    )
    merged = merge_global_figures_into_visual_plan(
        visual_plan_path=visual_plan_path,
        global_figure_plan_path=global_plan_path,
        blueprint_path=blueprint_path,
    )
    requests = merged["conceptual_figure_requests"]
    assert any(
        item.get("global_figure_id") == "GF01" for item in requests
    )
    assert any(
        item.get("global_figure_id") == "GF04" for item in requests
    )


def test_publication_uses_explicit_completion_metadata_without_duplication(
    tmp_path: Path,
):
    blueprint_path, _, blueprint = _make_completion_fixture(tmp_path)
    completion_path = tmp_path / "ARTICLE_COMPLETION_PACKAGE.json"
    completion_path.write_text(
        json.dumps(
            {
                "title": "Explicit completed review title",
                "abstract": "Explicit completed review abstract.",
            }
        ),
        encoding="utf-8",
    )
    package_path = tmp_path / "CONTENT_PACKAGE.json"
    package_path.write_text(
        json.dumps(
            {
                "artifacts": {
                    "review_blueprint": str(blueprint_path),
                    "article_completion_package": str(completion_path),
                }
            }
        ),
        encoding="utf-8",
    )
    metadata, warnings = resolve_publication_metadata(
        content_package_path=package_path
    )
    assert metadata["title"] == "Explicit completed review title"
    assert metadata["abstract"] == "Explicit completed review abstract."
    assert "abstract_inferred_from_review_blueprint" not in warnings

    body = _strip_embedded_title_and_abstract(
        "# Explicit completed review title\n\n"
        "## Abstract\n\nExplicit completed review abstract.\n\n"
        "## Introduction\n\nThe real body begins here."
    )
    assert "Explicit completed review abstract" not in body
    assert body.startswith("## Introduction")


def test_article_synthesis_fingerprint_changes_with_body_memory(tmp_path: Path):
    blueprint_path, sections_root, _ = _make_completion_fixture(tmp_path)
    first = collect_article_synthesis_inputs(blueprint_path, sections_root)
    draft = sections_root / "S03" / "SECTION_DRAFT_EN.md"
    draft.write_text(
        draft.read_text(encoding="utf-8")
        + " A newly revised bounded conclusion changes the article memory.",
        encoding="utf-8",
    )
    second = collect_article_synthesis_inputs(blueprint_path, sections_root)
    assert first["input_fingerprint"] != second["input_fingerprint"]


def test_structure_audit_accepts_equivalent_outlook_and_commander_order(
    tmp_path: Path,
):
    blueprint = _sample_blueprint()
    blueprint["article_rhetorical_contract"] = _native_contract(blueprint)
    blueprint["sections"][-1]["title"] = "Constraints and Future Outlook"
    blueprint_path = tmp_path / "REVIEW_BLUEPRINT.json"
    blueprint_path.write_text(json.dumps(blueprint), encoding="utf-8")
    manuscript_path = tmp_path / "FINAL_REVIEW_EN.md"
    order = [
        *blueprint["sections"][:-2],
        blueprint["sections"][-1],
        blueprint["sections"][-2],
    ]
    manuscript_path.write_text(
        "\n\n".join(
            [
                "# A review",
                "## Abstract\n\nThe abstract states the article scope.",
                "## Introduction\n\nThe introduction states the article scope.",
                *[
                    f"## {section['title']}\n\nThis section advances its bounded scientific argument."
                    for section in order
                ],
                "## Conclusion\n\nThe conclusion answers the central question.",
            ]
        ),
        encoding="utf-8",
    )

    report = audit_complete_manuscript(
        manuscript_path=manuscript_path,
        body_review_path=manuscript_path,
        completion_package_path=tmp_path / "missing_completion_package.json",
        blueprint_path=blueprint_path,
        output_path=tmp_path / "ARTICLE_STRUCTURE_AUDIT.json",
        expected_section_order=[section["section_id"] for section in order],
        body_is_complete_manuscript=True,
    )

    assert report["status"] in {"passed", "needs_attention"}
    assert not report["blocking_flags"]
    assert report["summary"]["expected_section_order"] == [
        section["section_id"] for section in order
    ]


def test_structure_audit_accepts_equivalent_outlook_and_commander_order(
    tmp_path: Path,
):
    blueprint = _sample_blueprint()
    blueprint["article_rhetorical_contract"] = _native_contract(blueprint)
    blueprint["sections"][-1]["title"] = "Constraints and Future Outlook"
    blueprint_path = tmp_path / "REVIEW_BLUEPRINT.json"
    blueprint_path.write_text(json.dumps(blueprint), encoding="utf-8")
    manuscript_path = tmp_path / "FINAL_REVIEW_EN.md"
    order = [*blueprint["sections"][:-2], blueprint["sections"][-1], blueprint["sections"][-2]]
    manuscript_path.write_text(
        "\n\n".join(
            [
                "# A review",
                "## Abstract\n\nThe abstract states the article scope.",
                "## Introduction\n\nThe introduction states the article scope.",
                *[
                    f"## {section['title']}\n\nThis section advances its bounded scientific argument."
                    for section in order
                ],
                "## Conclusion\n\nThe conclusion answers the central question.",
            ]
        ),
        encoding="utf-8",
    )

    report = audit_complete_manuscript(
        manuscript_path=manuscript_path,
        body_review_path=manuscript_path,
        completion_package_path=tmp_path / "missing_completion_package.json",
        blueprint_path=blueprint_path,
        output_path=tmp_path / "ARTICLE_STRUCTURE_AUDIT.json",
        expected_section_order=[section["section_id"] for section in order],
        body_is_complete_manuscript=True,
    )

    assert report["status"] in {"passed", "needs_attention"}
    assert not report["blocking_flags"]
    assert report["summary"]["expected_section_order"] == [
        section["section_id"] for section in order
    ]
