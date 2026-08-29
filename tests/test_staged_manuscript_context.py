"""Focused tests for staged manuscript context preparation."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping

import pytest

import scripts.build_staged_manuscript_context as cli
import scripts.run_staged_article_completion as staged_cli
from optomind_research.runtime.staged_article_completion import (
    STAGE_ORDER,
    StagedArticleCompletionState,
    StagedStageState,
    run_staged_article_completion,
)
from optomind_research.runtime.full_manuscript_handoff import (
    build_full_manuscript_handoff,
)
from optomind_research.runtime.staged_manuscript_context import (
    GLOBAL_INPUTS_JSON,
    SCHEMA_VERSION,
    STAGE_INPUTS_JSON,
    STAGE_KEYS,
    StagedContextError,
    build_staged_manuscript_context,
)


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory."""
    root = (
        Path(__file__).resolve().parents[1]
        / ".pytest-basetemp-staged-manuscript-context"
    )
    root.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)[:40]
    path = root / f"{safe_name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def _write_section(
    root: Path,
    section_id: str,
    title: str,
    *,
    with_review: bool = True,
    block_indices: list[int | None] | None = None,
) -> dict[str, str]:
    asset_dir = root / f"enhanced_{section_id}"
    asset_dir.mkdir(parents=True, exist_ok=True)
    (asset_dir / "ENHANCED_CHAPTER.md").write_text(
        f"# {title}\n\n"
        f"Paragraph one of {section_id} with [REF:doi:10.1000/x].\n\n"
        f"Paragraph two of {section_id} with [REF:paper-1].",
        encoding="utf-8",
    )
    plan = {
        "schema_version": "chapter_asset_enhancer.v1",
        "section_id": section_id,
        "plan": {
            "title": title,
            "chapter_thesis": f"Thesis {section_id}",
            "reader_takeaway": f"Takeaway {section_id}",
            "argument_sequence": [
                {"step_index": 1, "purpose": f"Purpose {section_id}"}
            ],
            "terminology_rows": [{"term": "PINN", "definition": "physics-informed"}],
        },
        "warnings": [],
    }
    (asset_dir / "CHAPTER_ARGUMENT_PLAN.json").write_text(
        json.dumps(plan), encoding="utf-8"
    )
    (asset_dir / "CLAIM_TO_PARAGRAPH_MAP.json").write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "section_id": section_id,
                "claim_to_paragraph": {},
                "block_to_claim": [],
            }
        ),
        encoding="utf-8",
    )
    if block_indices is None:
        block_indices = [1, 2]
    blocks = []
    for local_index, block_index in enumerate(block_indices, start=1):
        block: dict[str, Any] = {
            "title": f"Block {local_index}",
            "prose": f"Block {local_index} prose {section_id}.",
            "goal": f"goal {local_index}",
            "markers": ["doi:10.1000/x"] if local_index == 1 else ["paper-1"],
            "claim_handles": ["C01"] if local_index == 1 else [],
            "evidence_handles": ["E01"] if local_index == 1 else [],
        }
        if block_index is not None:
            block["block_index"] = block_index
        blocks.append(block)
    (asset_dir / "EXPLANATION_BLOCKS.json").write_text(
        json.dumps({"schema_version": "v1", "blocks": blocks}),
        encoding="utf-8",
    )
    ledger = {
        "schema_version": "v1",
        "section_id": section_id,
        "records": [
            {
                "handle": f"X{section_id}_A",
                "marker_id": "ledger-a",
                "role": "explanatory_context",
                "permission": "background_explanation_only",
                "selection_score": 1.0,
                "helpfulness_score": 2.0,
                "overlaps_core_reference": False,
                "metadata": {
                    "title": "Ledger A",
                    "paper_id": "s2:a",
                    "doi": "10.1/a",
                    "abstract": "Abstract A",
                    "year": "2023",
                    "venue": "Journal A",
                    "authors": ["Alice", "Bob"],
                },
            },
            {
                "handle": f"X{section_id}_B",
                "marker_id": "ledger-b",
                "role": "explanatory_context",
                "permission": "background_explanation_only",
                "selection_score": 1.0,
                "helpfulness_score": 1.0,
                "overlaps_core_reference": True,
                "metadata": {
                    "title": "Ledger B",
                    "paper_id": "s2:b",
                },
            },
            {
                "handle": f"X{section_id}_C",
                "marker_id": "ledger-c",
                "role": "explanatory_context",
                "permission": "background_explanation_only",
                "selection_score": 5.0,
                "helpfulness_score": 0.5,
                "overlaps_core_reference": False,
                "metadata": {
                    "title": "Ledger C",
                    "paper_id": "s2:c",
                },
            },
            {
                "handle": f"X{section_id}_D",
                "marker_id": "ledger-a",
                "role": "explanatory_context",
                "permission": "background_explanation_only",
                "selection_score": 0.0,
                "helpfulness_score": 1.0,
                "overlaps_core_reference": False,
                "metadata": {
                    "title": "Ledger A duplicate",
                    "paper_id": "s2:a",
                },
            },
            {
                "handle": f"X{section_id}_E",
                "marker_id": "ledger-e",
                "role": "explanatory_context",
                "permission": "background_explanation_only",
                "helpfulness_score": 3.0,
                "overlaps_core_reference": False,
                "metadata": {
                    "title": "Ledger E",
                    "paper_id": "s2:e",
                },
            },
        ],
    }
    (asset_dir / "EXPLANATORY_CITATION_LEDGER.json").write_text(
        json.dumps(ledger), encoding="utf-8"
    )
    (asset_dir / "ENHANCEMENT_REPORT.json").write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "section_id": section_id,
                "status": "enhanced",
                "word_counts": {"enhanced": 20},
                "hard_defects": {},
            }
        ),
        encoding="utf-8",
    )
    if with_review:
        (asset_dir / "BLOCK_SCIENTIFIC_REVIEW.json").write_text(
            json.dumps(
                {
                    "schema_version": "v1",
                    "section_id": section_id,
                    "attempted": True,
                    "available": True,
                    "advisory_count": 1,
                    "blocking_count": 0,
                    "comments": [
                        {
                            "block_index": 1,
                            "blocking": False,
                            "flag_type": "clarity",
                            "issue": "reword",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    packet = {
        "schema_version": "s04_acceptance.v1",
        "section_id": section_id,
        "claims": [],
        "manuscript_context": {
            "global_review_thesis": "Global review thesis",
            "global_narrative_strategy": "Global narrative strategy",
            "research_context": {
                "user_question": "Compare PINN methods with differentiable solvers.",
                "problem_understanding": "understand credibility",
            }
        },
        "section_contract": {
            "central_thesis": f"Global thesis via {section_id}",
            "argument_role": {"statement": f"Narrative {section_id}"},
            "section_purpose": f"Purpose {section_id}",
        },
        "evidence_packets": [
            {"paper_id": "paper-1", "source_title": "Paper One", "chunk_id": "c1"},
        ],
        "literature_coverage": {
            "sources": [{"paper_id": "paper-2", "title": "Paper Two"}]
        },
    }
    packet_path = root / f"packet_{section_id}.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    return {
        "enhanced_asset_dir": str(asset_dir),
        "authoritative_input_packet": str(packet_path),
    }


def _build_handoff(root: Path, section_ids: list[str]) -> Path:
    sections = [
        {
            "section_id": section_id,
            **_write_section(root, section_id, f"Title {section_id}"),
        }
        for section_id in section_ids
    ]
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "optomind.full_manuscript_handoff.manifest.v2",
                "project_root": str(root),
                "sections": sections,
            }
        ),
        encoding="utf-8",
    )
    handoff_dir = root / "handoff_out"
    build_full_manuscript_handoff(
        manifest_path=manifest, output_dir=handoff_dir
    )
    return handoff_dir / "UNIFIED_MANUSCRIPT_HANDOFF.json"


def _write_work_order(root: Path, section_ids: list[str]) -> Path:
    path = root / "work_order.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": (
                    "optomind.global_manuscript_commander.work_order.v2"
                ),
                "status": "completed",
                "fingerprint": "wo-fp",
                "manuscript_diagnosis": "diagnosis",
                "proposed_section_order": [
                    {"section_id": section_id, "position": index}
                    for index, section_id in enumerate(section_ids)
                ],
                "section_decisions": [
                    {"section_id": section_id, "decision": "mechanism"}
                    for section_id in section_ids
                ],
                "structure_gaps": [],
                "missing_axes": [],
                "cross_section_conflicts": [],
                "visual_work_orders": [],
                "retained_advisory_issues": [
                    {"statement_id": "x", "reason": "advisory"}
                ],
                "read_only_declaration": {
                    "chapter_text_changed": False,
                    "retrieval_launched": False,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _build_context(
    root: Path, section_ids: list[str], output_dir: Path
) -> dict[str, Any]:
    return build_staged_manuscript_context(
        project_root=root,
        handoff_path=_build_handoff(root, section_ids),
        commander_work_order_path=_write_work_order(root, section_ids),
        output_dir=output_dir,
    )


def test_stage_selection_and_soft_targets(tmp_path: Path) -> None:
    summary = _build_context(tmp_path, ["S01", "S02"], tmp_path / "ctx")
    stage_inputs = json.loads(
        (tmp_path / "ctx" / STAGE_INPUTS_JSON).read_text(encoding="utf-8")
    )
    stages = stage_inputs["stages"]
    assert list(stages.keys()) == list(STAGE_KEYS)
    assert stages["conclusion"]["soft_word_target"] == {
        "min": 500,
        "max": 900,
    }
    assert stages["introduction"]["soft_word_target"] == {
        "min": 800,
        "max": 1300,
    }
    assert stages["abstract"]["soft_word_target"] == {
        "min": 220,
        "max": 300,
    }
    assert "soft_word_target" not in stages["whole_manuscript_review"]
    assert "soft_word_target" not in stages["bounded_patch_proposals"]
    assert "soft_word_target" not in stages["editorial_revision"]
    assert summary["schema_version"] == SCHEMA_VERSION


def test_global_inputs_include_fail_open_presentation_ir(tmp_path: Path) -> None:
    _build_context(tmp_path, ["S01"], tmp_path / "ctx")
    global_inputs = json.loads(
        (tmp_path / "ctx" / GLOBAL_INPUTS_JSON).read_text(encoding="utf-8")
    )
    ir = global_inputs["presentation_ir"]
    assert ir["schema_version"] == "optomind.literature_review_presentation_ir.v1"
    assert ir["central_topic"]
    assert ir["synthesis_claims"]
    assert ir["forbidden_claims"]


def test_full_text_reaches_conclusion_and_review_but_abstract_summaries(
    tmp_path: Path,
) -> None:
    _build_context(tmp_path, ["S01"], tmp_path / "ctx")
    stages = json.loads(
        (tmp_path / "ctx" / STAGE_INPUTS_JSON).read_text(encoding="utf-8")
    )["stages"]
    assert "Paragraph one of S01" in stages["conclusion"]["sections"][0][
        "full_text"
    ]
    assert "Paragraph one of S01" in stages["whole_manuscript_review"][
        "sections"
    ][0]["full_text"]
    assert stages["whole_manuscript_review"]["sections"][0]["blocks"][0][
        "prose"
    ]
    abstract_summaries = stages["abstract"]["section_summaries"]
    assert "full_text" not in abstract_summaries[0]
    assert abstract_summaries[0]["block_ids"] == ["S01-B001", "S01-B002"]


def test_stable_block_ids_and_hashes(tmp_path: Path) -> None:
    _build_context(tmp_path, ["S01"], tmp_path / "ctx")
    global_inputs = json.loads(
        (tmp_path / "ctx" / GLOBAL_INPUTS_JSON).read_text(encoding="utf-8")
    )
    blocks = global_inputs["sections"][0]["blocks"]
    assert [block["block_id"] for block in blocks] == ["S01-B001", "S01-B002"]
    assert all(re.fullmatch(r"[0-9a-f]{64}", block["sha256"]) for block in blocks)
    assert blocks[0]["sha256"] != blocks[1]["sha256"]

    _build_context(tmp_path, ["S01"], tmp_path / "ctx2")
    second = json.loads(
        (tmp_path / "ctx2" / GLOBAL_INPUTS_JSON).read_text(encoding="utf-8")
    )
    assert second["sections"][0]["blocks"] == blocks


def test_local_background_ranking_dedupe_and_trust_boundary(
    tmp_path: Path,
) -> None:
    _build_context(tmp_path, ["S01", "S02"], tmp_path / "ctx")
    global_inputs = json.loads(
        (tmp_path / "ctx" / GLOBAL_INPUTS_JSON).read_text(encoding="utf-8")
    )
    candidates = global_inputs["local_background_candidates"]
    assert [candidate["citation_id"] for candidate in candidates] == [
        "ledger-e",
        "ledger-a",
        "ledger-b",
        "ledger-c",
    ]
    assert candidates[0]["selection_score"] is None
    assert candidates[0]["helpfulness_score"] == 3.0
    assert candidates[1]["citation_id"] == "ledger-a"
    assert candidates[1]["helpfulness_score"] == 2.0
    assert candidates[2]["citation_id"] == "ledger-b"
    assert candidates[3]["selection_score"] == 5.0  # helpfulness ranks it last
    assert all(
        candidate["trust_type"] == "background_explanation_only"
        for candidate in candidates
    )
    assert all(
        "helpfulness_score" in candidate and "selection_score" in candidate
        for candidate in candidates
    )
    intro_candidates = json.loads(
        (tmp_path / "ctx" / STAGE_INPUTS_JSON).read_text(encoding="utf-8")
    )["stages"]["introduction"]["local_background_candidates"]
    assert intro_candidates == candidates


def test_global_values_from_manuscript_context_and_mapping_role(
    tmp_path: Path,
) -> None:
    _build_context(tmp_path, ["S01"], tmp_path / "ctx")
    global_inputs = json.loads(
        (tmp_path / "ctx" / GLOBAL_INPUTS_JSON).read_text(encoding="utf-8")
    )
    assert global_inputs["global_review_thesis"] == "Global review thesis"
    assert global_inputs["global_narrative_strategy"] == (
        "Global narrative strategy"
    )
    assert global_inputs["sections"][0]["narrative_strategy"] == (
        "Narrative S01"  # mapping argument_role -> statement
    )


def test_block_index_duplicate_rejected_and_fallback_enumeration(
    tmp_path: Path,
) -> None:
    sections = [
        {
            "section_id": "S01",
            **_write_section(
                tmp_path, "S01", "Title", block_indices=[1, 1]
            ),
        }
    ]
    manifest = tmp_path / "bad_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "optomind.full_manuscript_handoff.manifest.v2",
                "project_root": str(tmp_path),
                "sections": sections,
            }
        ),
        encoding="utf-8",
    )
    handoff_out = tmp_path / "bad_handoff"
    build_full_manuscript_handoff(
        manifest_path=manifest, output_dir=handoff_out
    )
    with pytest.raises(StagedContextError, match="block_index"):
        build_staged_manuscript_context(
            project_root=tmp_path,
            handoff_path=handoff_out / "UNIFIED_MANUSCRIPT_HANDOFF.json",
            commander_work_order_path=_write_work_order(tmp_path, ["S01"]),
            output_dir=tmp_path / "ctx_bad",
        )

    missing_sections = [
        {
            "section_id": "S01",
            **_write_section(
                tmp_path, "S01", "Title", block_indices=[None, None]
            ),
        }
    ]
    manifest_ok = tmp_path / "ok_manifest.json"
    manifest_ok.write_text(
        json.dumps(
            {
                "schema_version": "optomind.full_manuscript_handoff.manifest.v2",
                "project_root": str(tmp_path),
                "sections": missing_sections,
            }
        ),
        encoding="utf-8",
    )
    handoff_ok = tmp_path / "ok_handoff"
    build_full_manuscript_handoff(
        manifest_path=manifest_ok, output_dir=handoff_ok
    )
    build_staged_manuscript_context(
        project_root=tmp_path,
        handoff_path=handoff_ok / "UNIFIED_MANUSCRIPT_HANDOFF.json",
        commander_work_order_path=_write_work_order(tmp_path, ["S01"]),
        output_dir=tmp_path / "ctx_ok",
    )
    global_inputs = json.loads(
        (tmp_path / "ctx_ok" / GLOBAL_INPUTS_JSON).read_text(
            encoding="utf-8"
        )
    )
    blocks = global_inputs["sections"][0]["blocks"]
    assert [block["block_id"] for block in blocks] == [
        "S01-B001",
        "S01-B002",
    ]
    assert [block["local_index"] for block in blocks] == [1, 2]


def _cli_fake_state() -> StagedArticleCompletionState:
    return StagedArticleCompletionState(
        run_id="cli",
        run_fingerprint="fp",
        stage_order=list(STAGE_ORDER),
        status="completed",
        stages={
            "conclusion": StagedStageState(
                stage="conclusion",
                status="completed",
                usage={"input_tokens": 1, "output_tokens": 1},
            )
        },
    )


def test_cli_unwraps_staged_stage_inputs_wrapper(
    tmp_path: Path, monkeypatch
) -> None:
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(json.dumps({"topic": "x"}), encoding="utf-8")
    wrapper_path = tmp_path / "stage_inputs.json"
    wrapper_path.write_text(
        json.dumps(
            {
                "schema_version": "optomind.staged_manuscript_context.v1",
                "stages": {
                    "conclusion": {"kind": "conclusion-selected"},
                    "introduction": {"kind": "introduction-selected"},
                },
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return _cli_fake_state()

    monkeypatch.setattr(
        staged_cli, "run_staged_article_completion", fake_run
    )
    code = staged_cli.main(
        [
            "--inputs-json",
            str(inputs_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--stage-inputs-json",
            str(wrapper_path),
        ]
    )
    assert code == 0
    assert captured["stage_inputs"] == {
        "conclusion": {"kind": "conclusion-selected"},
        "introduction": {"kind": "introduction-selected"},
    }

    flat_path = tmp_path / "flat.json"
    flat_path.write_text(
        json.dumps({"conclusion": {"kind": "flat"}}), encoding="utf-8"
    )
    captured.clear()
    code_flat = staged_cli.main(
        [
            "--inputs-json",
            str(inputs_path),
            "--output-dir",
            str(tmp_path / "out2"),
            "--stage-inputs-json",
            str(flat_path),
        ]
    )
    assert code_flat == 0
    assert captured["stage_inputs"] == {"conclusion": {"kind": "flat"}}


def test_builder_output_to_cli_uses_selected_stage_inputs(
    tmp_path: Path, capsys
) -> None:
    _build_context(tmp_path, ["S01", "S02"], tmp_path / "ctx")
    stage_inputs_path = tmp_path / "ctx" / STAGE_INPUTS_JSON
    wrapper = json.loads(stage_inputs_path.read_text(encoding="utf-8"))
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(json.dumps({"topic": "global"}), encoding="utf-8")

    cli_out = tmp_path / "cli_out"
    code = staged_cli.main(
        [
            "--inputs-json",
            str(inputs_path),
            "--output-dir",
            str(cli_out),
            "--stage-inputs-json",
            str(stage_inputs_path),
        ]
    )
    assert code == 0
    cli_conclusion = json.loads(
        (cli_out / "staged_conclusion.json").read_text(encoding="utf-8")
    )["input_fingerprint"]
    cli_introduction = json.loads(
        (cli_out / "staged_introduction.json").read_text(encoding="utf-8")
    )["input_fingerprint"]

    direct_out = tmp_path / "direct_out"
    run_staged_article_completion(
        work_dir=direct_out,
        inputs={"topic": "global"},
        stage_inputs=wrapper["stages"],
        run_id="",
    )
    direct_conclusion = json.loads(
        (direct_out / "staged_conclusion.json").read_text(encoding="utf-8")
    )["input_fingerprint"]
    direct_introduction = json.loads(
        (direct_out / "staged_introduction.json").read_text(encoding="utf-8")
    )["input_fingerprint"]
    assert cli_conclusion == direct_conclusion
    assert cli_introduction == direct_introduction

    fallback_out = tmp_path / "fallback_out"
    run_staged_article_completion(
        work_dir=fallback_out,
        inputs={"topic": "global"},
        run_id="",
    )
    fallback_conclusion = json.loads(
        (fallback_out / "staged_conclusion.json").read_text(encoding="utf-8")
    )["input_fingerprint"]
    assert cli_conclusion != fallback_conclusion


def test_digest_mismatch_refusal(tmp_path: Path) -> None:
    handoff_path = _build_handoff(tmp_path, ["S01"])
    work_order_path = _write_work_order(tmp_path, ["S01"])
    enhanced = tmp_path / "enhanced_S01" / "ENHANCED_CHAPTER.md"
    enhanced.write_text("# Title\n\nmutated content", encoding="utf-8")

    with pytest.raises(StagedContextError, match="digest mismatch"):
        build_staged_manuscript_context(
            project_root=tmp_path,
            handoff_path=handoff_path,
            commander_work_order_path=work_order_path,
            output_dir=tmp_path / "ctx",
        )


def test_relocation_safe_fingerprint(tmp_path: Path) -> None:
    first = _build_context(tmp_path, ["S01"], tmp_path / "ctx1")

    root2 = tmp_path / "other_root"
    root2.mkdir()
    shutil.copytree(tmp_path / "enhanced_S01", root2 / "enhanced_S01")
    shutil.copy2(tmp_path / "packet_S01.json", root2 / "packet_S01.json")
    second = _build_context(root2, ["S01"], root2 / "ctx2")

    assert first["input_fingerprint"] == second["input_fingerprint"]


def test_no_evidence_promotion_and_citation_inventory(
    tmp_path: Path,
) -> None:
    _build_context(tmp_path, ["S01"], tmp_path / "ctx")
    global_inputs = json.loads(
        (tmp_path / "ctx" / GLOBAL_INPUTS_JSON).read_text(encoding="utf-8")
    )
    inventory = global_inputs["citation_inventory"]
    core = [entry for entry in inventory if entry["trust_type"] == "core_evidence"]
    background = [
        entry for entry in inventory if entry["trust_type"] == "background_explanation_only"
    ]
    assert core and background
    assert all(entry["trust_type"] == "core_evidence" for entry in core)
    assert all(
        entry["trust_type"] == "background_explanation_only"
        for entry in background
    )
    assert any(entry["citation_id"] == "ledger-a" for entry in background)
    assert "paper-1" in {entry["citation_id"] for entry in core}


def test_cli_builds_and_validates_files(tmp_path: Path, capsys) -> None:
    handoff_path = _build_handoff(tmp_path, ["S01"])
    work_order_path = _write_work_order(tmp_path, ["S01"])
    code = cli.main(
        [
            "--project-root",
            str(tmp_path),
            "--handoff-json",
            str(handoff_path),
            "--commander-work-order-json",
            str(work_order_path),
            "--output-dir",
            str(tmp_path / "ctx"),
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["stage_keys"] == list(STAGE_KEYS)
    assert (tmp_path / "ctx" / GLOBAL_INPUTS_JSON).is_file()
    assert (tmp_path / "ctx" / STAGE_INPUTS_JSON).is_file()

    code_missing = cli.main(
        [
            "--project-root",
            str(tmp_path),
            "--handoff-json",
            str(tmp_path / "missing_handoff.json"),
            "--commander-work-order-json",
            str(work_order_path),
            "--output-dir",
            str(tmp_path / "ctx2"),
        ]
    )
    assert code_missing == 2
    assert "cannot read" in capsys.readouterr().err


def test_enriched_background_candidates_and_conclusion_identity(
    tmp_path: Path,
) -> None:
    _build_context(tmp_path, ["S01"], tmp_path / "ctx")
    global_inputs = json.loads(
        (tmp_path / "ctx" / GLOBAL_INPUTS_JSON).read_text(encoding="utf-8")
    )
    candidates = global_inputs["local_background_candidates"]
    by_id = {candidate["citation_id"]: candidate for candidate in candidates}
    ledger_a = by_id["ledger-a"]
    assert ledger_a["abstract"] == "Abstract A"
    assert ledger_a["year"] == "2023"
    assert ledger_a["venue"] == "Journal A"
    assert ledger_a["authors"] == ["Alice", "Bob"]
    assert ledger_a["doi"] == "10.1/a"
    assert ledger_a["paper_id"] == "s2:a"
    assert ledger_a["permission"] == "background_explanation_only"
    assert ledger_a["trust"] == "background_explanation_only"
    assert ledger_a["trust_type"] == "background_explanation_only"
    # no arbitrary cap: every unique ledger candidate survives dedupe.
    assert len(candidates) == len({candidate["citation_id"] for candidate in candidates})
    assert "ledger-a" in {candidate["citation_id"] for candidate in candidates}

    stages = json.loads(
        (tmp_path / "ctx" / STAGE_INPUTS_JSON).read_text(encoding="utf-8")
    )["stages"]
    conclusion = stages["conclusion"]
    assert conclusion["user_question"] == (
        "Compare PINN methods with differentiable solvers."
    )
    assert conclusion["problem_understanding"] == "understand credibility"
    assert conclusion["global_review_thesis"] == "Global review thesis"
    assert conclusion["global_narrative_strategy"] == (
        "Global narrative strategy"
    )
    assert conclusion["commander_structure"]["proposed_section_order"][0][
        "section_id"
    ] == "S01"

    introduction = stages["introduction"]
    assert introduction["problem_understanding"] == "understand credibility"
    assert introduction["global_review_thesis"] == "Global review thesis"
    assert introduction["global_narrative_strategy"] == (
        "Global narrative strategy"
    )
    intro_candidates = introduction["local_background_candidates"]
    assert {candidate["citation_id"] for candidate in intro_candidates} == {
        candidate["citation_id"] for candidate in candidates
    }
    intro_by_id = {
        candidate["citation_id"]: candidate for candidate in intro_candidates
    }
    assert intro_by_id["ledger-a"]["abstract"] == "Abstract A"

    abstract_identity = stages["abstract"]["article_identity"]
    assert abstract_identity["user_question"] == (
        "Compare PINN methods with differentiable solvers."
    )
    assert abstract_identity["global_review_thesis"] == "Global review thesis"


def test_editorial_revision_stage_input_and_full_text_authority(
    tmp_path: Path,
) -> None:
    _build_context(tmp_path, ["S01"], tmp_path / "ctx")
    stages = json.loads(
        (tmp_path / "ctx" / STAGE_INPUTS_JSON).read_text(encoding="utf-8")
    )["stages"]
    editorial = stages["editorial_revision"]
    assert editorial["section_order"] == ["S01"]
    assert editorial["commander_structure"]["proposed_section_order"][0][
        "section_id"
    ] == "S01"
    assert editorial["reviewer_sources"] == (
        "previous_artifacts.whole_manuscript_review"
    )
    assert editorial["patch_sources"] == (
        "previous_artifacts.bounded_patch_proposals"
    )
    section = editorial["sections"][0]
    # final enhanced chapter prose is authoritative; historical review
    # summaries stay advisory and never replace full_text.
    assert section["full_text"] == (
        "# Title S01\n\n"
        "Paragraph one of S01 with [REF:doi:10.1000/x].\n\n"
        "Paragraph two of S01 with [REF:paper-1]."
    )
    assert section["blocks"][0]["block_id"] == "S01-B001"
    global_inputs = json.loads(
        (tmp_path / "ctx" / GLOBAL_INPUTS_JSON).read_text(encoding="utf-8")
    )
    review_summary = global_inputs["sections"][0]["review_summary"]
    assert review_summary["available"] is True
    assert review_summary["comments"][0]["issue"] == "reword"
    assert "reword" not in section["full_text"]


def test_whole_manuscript_review_stage_input_plan_context_no_historical_summary(
    tmp_path: Path,
) -> None:
    _build_context(tmp_path, ["S01"], tmp_path / "ctx")
    stages = json.loads(
        (tmp_path / "ctx" / STAGE_INPUTS_JSON).read_text(encoding="utf-8")
    )["stages"]
    review_input = stages["whole_manuscript_review"]
    assert review_input["commander_structure"]["proposed_section_order"][0][
        "section_id"
    ] == "S01"
    assert review_input["user_question"] == (
        "Compare PINN methods with differentiable solvers."
    )
    assert review_input["global_review_thesis"] == "Global review thesis"
    assert review_input["global_narrative_strategy"] == (
        "Global narrative strategy"
    )
    section = review_input["sections"][0]
    # Historical per-section evidence review must not be reopened here.
    assert "review_summary" not in section
    assert "comments" not in section
    # Current manuscript prose and stable blocks stay authoritative.
    assert section["full_text"] == (
        "# Title S01\n\n"
        "Paragraph one of S01 with [REF:doi:10.1000/x].\n\n"
        "Paragraph two of S01 with [REF:paper-1]."
    )
    assert section["blocks"][0]["block_id"] == "S01-B001"
    assert section["blocks"][1]["block_id"] == "S01-B002"
