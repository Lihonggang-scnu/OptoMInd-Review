"""Focused offline tests for the standalone chapter asset enhancer."""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import optomind_research.chapter_asset_enhancer as enhancer
import scripts.run_chapter_asset_enhancer as cli


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory (no runtime directory listing)."""
    base = Path(tempfile.gettempdir()) / "optomind-chapter-asset-enhancer-tests"
    base.mkdir(exist_ok=True)
    path = base / f"{request.node.name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def _fake_packet_data() -> dict:
    return {
        "section_id": "S01",
        "section_contract": {
            "title": "Physics-Informed Neural Networks",
            "section_purpose": "Establish the mechanism and boundaries of PINNs.",
            "central_thesis": "PINNs translate governing equations into trainable residual constraints.",
            "argument_role": "Define, explain, and apply the method.",
            "forbidden_overclaims": ["Do not claim general superiority."],
            "scope_guardrails": ["Stay inside the reviewed electromagnetic and heat-transfer cases."],
            "open_questions": ["Which boundary conditions remain difficult to recover?"],
        },
        "claims": [
            {
                "claim_id": "C1",
                "statement": "PINNs embed governing equations as residual loss terms.",
                "statement_for_writing": "The reviewed studies report that PINNs embed governing equations as residual loss terms.",
                "writing_permission": "factual_assertion",
                "evidence_binding_status": "direct",
                "claim_state": "ready_for_write",
                "supported_components": ["loss residual"],
                "missing_evidence_components": [],
                "caveats": [],
            },
            {
                "claim_id": "C2",
                "statement": "PINNs recover missing boundary conditions in heat transfer.",
                "statement_for_writing": "The reviewed study reports that PINNs recover missing boundary conditions in heat transfer.",
                "writing_permission": "hedged_factual_assertion",
                "evidence_binding_status": "direct",
                "claim_state": "ready_for_write",
                "supported_components": ["boundary recovery"],
                "missing_evidence_components": [],
                "caveats": ["Limited to the demonstrated benchmark."],
            },
            {
                "claim_id": "C3",
                "statement": "PINNs outperform all classical solvers.",
                "statement_for_writing": "Unsupported broad superiority claim.",
                "writing_permission": "evidence_gap_only",
                "evidence_binding_status": "none",
                "claim_state": "blocked",
                "supported_components": [],
                "missing_evidence_components": ["comparative benchmark evidence"],
                "caveats": [],
            },
        ],
        "evidence_packets": [
            {
                "claim_id": "C1",
                "paper_id": "paper-s2-001",
                "chunk_id": "s2chunk:001",
                "exact_spans": ["PINNs embed governing equations as residual penalty terms in the training loss."],
                "visual_refs": [],
                "support_relation": "component_support",
                "limitations": ["Do not generalize to all physics-augmented methods."],
                "evidence_level": "structured_snippet",
                "source_kind": "s2_body_snippet",
                "scope_fit": "in_domain",
                "retrieval_role": "evidence_candidate",
                "source_title": "Physics-Informed Neural Network Survey",
            },
            {
                "claim_id": "C1",
                "paper_id": "paper-oa-002",
                "chunk_id": "oa-chunk:002",
                "exact_spans": ["The method enforces governing laws directly inside the training objective."],
                "visual_refs": [],
                "support_relation": "component_support",
                "limitations": ["Benchmark-specific."],
                "evidence_level": "fulltext",
                "source_kind": "oa_fulltext",
                "scope_fit": "in_domain",
                "retrieval_role": "evidence_candidate",
                "source_title": "Open-Access PINN Benchmark",
            },
            {
                "claim_id": "C2",
                "paper_id": "paper-ht-003",
                "chunk_id": "fulltext:003",
                "exact_spans": ["PINNs recover missing boundary conditions from sparse sensor data."],
                "visual_refs": [],
                "support_relation": "component_support",
                "limitations": ["Demonstrated on a heat-transfer benchmark."],
                "evidence_level": "fulltext",
                "source_kind": "fulltext",
                "scope_fit": "in_domain",
                "retrieval_role": "evidence_candidate",
                "source_title": "Heat Transfer Boundary Recovery",
            },
        ],
        "contradictions": [],
        "open_questions": ["Which boundary conditions remain difficult to recover?"],
        "transition_contract": {},
        "uncited_load_bearing_claim_ids": [],
        "visual_evidence": [],
        "visual_gap_plan": [],
        "manuscript_context": {},
        "literature_coverage": {},
    }


def _write_sources(tmp_path: Path) -> tuple[Path, Path]:
    packet_path = tmp_path / "input_packet.json"
    old_path = tmp_path / "SECTION_DRAFT_EN.md"
    packet_path.write_text(
        json.dumps(_fake_packet_data(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    old_path.write_text(
        "PINNs embed governing equations in the loss. "
        "They also recover boundary conditions from sparse data.",
        encoding="utf-8",
    )
    return packet_path, old_path


def _ledger_for_fake_packet() -> enhancer.HandleLedger:
    packet = enhancer._rehydrate_packet(_fake_packet_data())
    return enhancer._build_handle_ledger(packet)


def _fake_caller(ledger: enhancer.HandleLedger):
    c1 = ledger.claim_by_id["C1"].handle
    c2 = ledger.claim_by_id["C2"].handle
    c3 = ledger.claim_by_id["C3"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    e2 = ledger.evidence_by_claim["C1"][1].handle
    e3 = ledger.evidence_by_claim["C2"][0].handle
    calls: list[dict] = []
    block_count = {"value": 0}

    def caller(agent_name: str, messages: list[dict], **kwargs):
        calls.append(
            {
                "agent_name": agent_name,
                "messages": messages,
                "kwargs": kwargs,
            }
        )
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 100,
            "output_tokens": 50,
            "success": True,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            plan = {
                "chapter_thesis": "PINNs translate governing equations into trainable residual constraints.",
                "reader_takeaway": "PINNs work by embedding physics in the loss.",
                "argument_sequence": [
                    {
                        "step_index": 1,
                        "purpose": "Define and motivate PINNs.",
                        "claim_handles": [c1],
                        "evidence_handles": [e1, e2],
                    },
                    {
                        "step_index": 2,
                        "purpose": "Show a representative application and boundary.",
                        "claim_handles": [c2],
                        "evidence_handles": [e3],
                    },
                ],
                "terminology_rows": [
                    {
                        "term": "PINN",
                        "definition_claim_handle": c1,
                        "representative_application_claim_handle": c2,
                        "note": "First-use definition from mechanism evidence.",
                        "explanation_status": "first_use_defined",
                    }
                ],
                "explanation_block_rows": [
                    {
                        "block_index": 1,
                        "title": "Definition and mechanism",
                        "block_type": "introduction_viewpoint",
                        "goal": "Define PINNs and explain the residual loss mechanism.",
                        "claim_handles": [c1],
                        "evidence_handles": [e1, e2],
                        "omitted_handle_reasons": {},
                    },
                    {
                        "block_index": 2,
                        "title": "Application and boundary",
                        "block_type": "scientific_consequence",
                        "goal": "Connect PINNs to heat-transfer boundary recovery.",
                        "claim_handles": [c2],
                        "evidence_handles": [e3],
                        "omitted_handle_reasons": {},
                    },
                ],
                "omitted_handle_reasons": {
                    c3: "Evidence-gap-only claim; folded into an open question."
                },
            }
            content = json.dumps(plan)
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            block_count["value"] += 1
            if block_count["value"] == 1:
                content = json.dumps(
                    {
                        "paragraph_prose": (
                            "A physics-informed neural network defines a loss "
                            "that includes governing-equation residuals "
                            "alongside data terms."
                        ),
                        "used_evidence_handles": [e1, e2],
                    }
                )
            else:
                content = json.dumps(
                    {
                        "paragraph_prose": (
                            "In heat transfer, the network recovers missing "
                            "boundary conditions from sparse sensor data."
                        ),
                        "used_evidence_handles": [e3],
                    }
                )
        elif agent_name == "ChapterAssetBlockScientificReviewerAgent":
            content = json.dumps({"review_rows": [], "notes": ""})
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {
                    "verdict": "gaps_found",
                    "gaps": [
                        {
                            "gap_id": "G01",
                            "old_draft_snippet": "recover boundary conditions",
                            "scientific_content": (
                                "The old draft notes recovery of missing "
                                "boundary data; the new chapter should retain "
                                "that application explicitly."
                            ),
                            "claim_handles": [c2],
                            "evidence_handles": [e3],
                            "affected_block_indices": [2],
                        }
                    ],
                    "notes": "One targeted application gap.",
                }
            )
        elif agent_name == "ChapterAssetLegacyGapPatchWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": (
                        "In heat transfer, the network recovers missing "
                        "boundary conditions from sparse sensor data, and the "
                        "benchmark shows the method is limited by data "
                        "availability."
                    ),
                    "used_evidence_handles": [e3],
                }
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    return caller, calls


def test_three_stage_sequence_semantic_handles_terminology_and_costs(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    caller, calls = _fake_caller(ledger)
    output = tmp_path / "out"

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=output,
        live=True,
        planner_tier="c_model",
        block_tier="c_model",
        auditor_tier="c2_model",
        patch_tier="c_model",
        qwen_caller=caller,
    )

    assert report["status"] == "enhanced"
    assert [call["agent_name"] for call in calls] == [
        "ChapterAssetArgumentPlannerAgent",
        "ChapterAssetExplanationBlockWriterAgent",
        "ChapterAssetExplanationBlockWriterAgent",
        "ChapterAssetLegacyGapAuditorAgent",
        "ChapterAssetLegacyGapPatchWriterAgent",
        "ChapterAssetBlockScientificReviewerAgent",
    ]

    block_prompt = next(
        call["messages"][-1]["content"]
        for call in calls
        if call["agent_name"] == "ChapterAssetExplanationBlockWriterAgent"
        and '"block_index": 1' in call["messages"][-1]["content"]
    )
    assert '"paper_id"' not in block_prompt
    assert '"chunk_id"' not in block_prompt
    assert "[REF:" not in block_prompt
    assert '"s2_body_snippet"' in block_prompt
    assert '"oa_fulltext"' in block_prompt
    assert '"chapter_outline"' in block_prompt
    assert '"previous_block"' in block_prompt
    assert '"next_block"' in block_prompt

    enhanced = (output / "ENHANCED_CHAPTER.md").read_text(encoding="utf-8")
    assert "[REF:paper-s2-001]" in enhanced
    assert "[REF:paper-oa-002]" in enhanced
    assert "[REF:paper-ht-003]" in enhanced
    assert "E0" not in enhanced
    assert "C0" not in enhanced

    plan = json.loads(
        (output / "CHAPTER_ARGUMENT_PLAN.json").read_text(encoding="utf-8")
    )
    assert plan["plan"]["chapter_thesis"]
    terminology = json.loads(
        (output / "TERMINOLOGY_REGISTRY.json").read_text(encoding="utf-8")
    )
    assert terminology["terminology_rows"][0]["term"] == "PINN"

    claim_map = json.loads(
        (output / "CLAIM_TO_PARAGRAPH_MAP.json").read_text(encoding="utf-8")
    )
    assert claim_map["claim_to_paragraph"]["C1"] == [1]
    assert claim_map["claim_to_paragraph"]["C2"] == [2]

    gap_audit = json.loads(
        (output / "LEGACY_GAP_AUDIT.json").read_text(encoding="utf-8")
    )
    assert gap_audit["audit"]["verdict"] == "gaps_found"
    assert gap_audit["patch_records"][0]["applied"] is True

    usage = report["model_usage"]
    assert usage["call_count"] == 6
    assert usage["total_input_tokens"] == 600
    assert usage["total_output_tokens"] == 300
    assert usage["total_estimated_cost_cny"] > 0
    assert report["block_scientific_review"]["attempted"] is True
    assert report["block_scientific_review"]["available"] is True
    assert report["block_scientific_review"]["blocking_count"] == 0
    assert report["artifact_paths"]["block_scientific_review"] == (
        "BLOCK_SCIENTIFIC_REVIEW.json"
    )
    metrics = report["coverage_metrics"]
    assert metrics["planned_usable_claim_count"] == 2
    assert metrics["actual_covered_claim_count"] == 2
    assert metrics["evidence_handle_count"] == 3
    assert metrics["visible_unique_reference_count"] == 3
    assert "C3" in {
        str(ledger.claim(handle).claim_id)
        for handle in metrics["claims_omitted_with_reasons"]
    }
    assert all(
        not str(value).startswith(("C:", "F:", "/"))
        for value in report["artifact_paths"].values()
    )


def test_soft_length_and_omission_warnings_do_not_close_gate(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    calls: list[dict] = []

    def caller(agent_name: str, messages: list[dict], **kwargs):
        calls.append(agent_name)
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 50,
            "output_tokens": 20,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [
                        {
                            "term": "PINN",
                            "definition_claim_handle": c1,
                            "representative_application_claim_handle": None,
                            "note": "Evidence-limited terminology.",
                            "explanation_status": "evidence_limited",
                        }
                    ],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Short block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "Short evidence-limited block.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetBlockScientificReviewerAgent":
            content = json.dumps({"review_rows": [], "notes": ""})
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-soft",
        live=True,
        qwen_caller=caller,
    )

    assert report["status"] == "enhanced"
    assert any("usable claim not assigned or omitted" in w for w in report["soft_warnings"])
    assert not any("quota" in str(w).lower() for w in report["soft_warnings"])
    assert report["model_usage"]["call_count"] == 4
    assert "Short evidence-limited block" in (
        tmp_path / "out-soft" / "ENHANCED_CHAPTER.md"
    ).read_text(encoding="utf-8")


def test_unknown_evidence_handle_is_preserved_without_repair_marker(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    calls: list[str] = []

    def caller(agent_name: str, messages: list[dict], **kwargs):
        calls.append(agent_name)
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 50,
            "output_tokens": 20,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "This must never become a citation.",
                    "used_evidence_handles": ["E99_UNKNOWN_HANDLE"],
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        elif agent_name == "ChapterAssetBlockScientificReviewerAgent":
            content = json.dumps({"review_rows": [], "notes": ""})
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-unknown",
        live=True,
        qwen_caller=caller,
    )

    assert report["status"] == "enhanced"
    assert report["fail_open_reason"] == ""
    assert calls == [
        "ChapterAssetArgumentPlannerAgent",
        "ChapterAssetExplanationBlockWriterAgent",
        "ChapterAssetLegacyGapAuditorAgent",
        "ChapterAssetBlockScientificReviewerAgent",
    ]
    enhanced = (
        tmp_path / "out-unknown" / "ENHANCED_CHAPTER.md"
    ).read_text(encoding="utf-8")
    assert "This must never become a citation." in enhanced
    assert not any(
        "unknown" in str(item).lower()
        for item in report.get("soft_warnings", [])
    )
    assert not any(
        "unknown" in json.dumps(item, ensure_ascii=False).lower()
        for item in report.get("recovery_diagnostics", [])
    )


def test_planner_model_failure_preserves_original_text(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)

    def caller(*_args, **_kwargs):
        raise RuntimeError("transport down")

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-fail",
        live=True,
        qwen_caller=caller,
    )

    assert report["status"] == "fail_open_original"
    assert "Core model response unavailable" in report["fail_open_reason"]
    assert (
        tmp_path / "out-fail" / "ENHANCED_CHAPTER.md"
    ).read_text(encoding="utf-8") == (
        "# Physics-Informed Neural Networks\n\n"
        + old_path.read_text(encoding="utf-8").strip()
        + "\n"
    )
    assert report["model_usage"]["call_count"] == 2
    assert report["recovery_diagnostics"][0]["repaired"] is False


def test_cli_help_and_overwrite_refusal(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--help"])
    assert exc_info.value.code == 0

    packet_path, old_path = _write_sources(tmp_path)
    existing = tmp_path / "existing-out"
    existing.mkdir()
    (existing / "keep.txt").write_text("keep", encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise AssertionError("writer must not be called on collision")

    with pytest.raises(FileExistsError, match="already exists"):
        cli.main(
            [
                "--packet-path",
                str(packet_path),
                "--old-draft",
                str(old_path),
                "--output-dir",
                str(existing),
                "--live",
            ],
            qwen_caller=boom,
        )
    assert (existing / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_cli_dry_run_writes_nothing(tmp_path: Path) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    output = tmp_path / "dry-out"

    code = cli.main(
        [
            "--packet-path",
            str(packet_path),
            "--old-draft",
            str(old_path),
            "--output-dir",
            str(output),
        ],
        qwen_caller=lambda *_args, **_kwargs: None,
    )

    assert code == 0
    assert not output.exists()


def test_planner_contract_repair_succeeds_once(tmp_path: Path) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    calls: list[dict] = []
    planner_attempts = {"count": 0}

    def caller(agent_name: str, messages: list[dict], **kwargs):
        calls.append(
            {"agent_name": agent_name, "messages": messages, "kwargs": kwargs}
        )
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 80,
            "output_tokens": 40,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            planner_attempts["count"] += 1
            if planner_attempts["count"] == 1:
                content = "not-json"
            else:
                content = json.dumps(
                    {
                        "chapter_thesis": "Thesis.",
                        "reader_takeaway": "Takeaway.",
                        "argument_sequence": [],
                        "terminology_rows": [],
                        "explanation_block_rows": [
                            {
                                "block_index": 1,
                                "title": "Block",
                                "block_type": "explanatory_body",
                                "goal": "Explain.",
                                "claim_handles": [c1],
                                "evidence_handles": [e1],
                                "omitted_handle_reasons": {},
                            }
                        ],
                        "omitted_handle_reasons": {},
                    }
                )
        elif agent_name == "ChapterAssetContractRepairAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "A PINN embeds physics in the loss.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetBlockScientificReviewerAgent":
            content = json.dumps({"review_rows": [], "notes": ""})
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-planner-repair",
        live=True,
        qwen_caller=caller,
    )

    assert report["status"] == "enhanced"
    assert [call["agent_name"] for call in calls] == [
        "ChapterAssetArgumentPlannerAgent",
        "ChapterAssetContractRepairAgent",
        "ChapterAssetExplanationBlockWriterAgent",
        "ChapterAssetLegacyGapAuditorAgent",
        "ChapterAssetBlockScientificReviewerAgent",
    ]
    assert report["recovery_diagnostics"][0]["repaired"] is True
    repair_message = calls[1]["messages"][-1]["content"]
    assert "allowed_semantic_handles" in repair_message
    assert "not-json" in repair_message


def test_block_without_evidence_is_preserved_without_contract_repair(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    calls: list[str] = []
    def caller(agent_name: str, *_args, **_kwargs):
        calls.append(agent_name)
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 70,
            "output_tokens": 30,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "A factual block with no evidence.",
                    "used_evidence_handles": [],
                }
            )
        elif agent_name == "ChapterAssetContractRepairAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "A PINN embeds physics in the loss.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetBlockScientificReviewerAgent":
            content = json.dumps({"review_rows": [], "notes": ""})
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-block-repair",
        live=True,
        qwen_caller=caller,
    )

    assert report["status"] == "enhanced"
    assert calls == [
        "ChapterAssetArgumentPlannerAgent",
        "ChapterAssetExplanationBlockWriterAgent",
        "ChapterAssetLegacyGapAuditorAgent",
        "ChapterAssetBlockScientificReviewerAgent",
    ]
    assert report["recovery_diagnostics"] == []
    assert "A factual block with no evidence." in (
        tmp_path / "out-block-repair" / "ENHANCED_CHAPTER.md"
    ).read_text(encoding="utf-8")


def test_claim_map_does_not_overreport_planned_coverage(tmp_path: Path) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    c2 = ledger.claim_by_id["C2"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    e2 = ledger.evidence_by_claim["C1"][1].handle

    def caller(agent_name: str, *_args, **_kwargs):
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 60,
            "output_tokens": 30,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1, c2],
                            "evidence_handles": [e1, e2],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "Only claim one evidence is used here.",
                    "used_evidence_handles": [e1, e2],
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-coverage",
        live=True,
        qwen_caller=caller,
    )
    claim_map = json.loads(
        (
            tmp_path
            / "out-coverage"
            / "CLAIM_TO_PARAGRAPH_MAP.json"
        ).read_text(encoding="utf-8")
    )
    assert claim_map["claim_to_paragraph"]["C1"] == [1]
    assert "C2" not in claim_map["claim_to_paragraph"]
    block = claim_map["block_to_claim"][0]
    assert set(block["planned_claim_ids"]) == {"C1", "C2"}
    assert block["covered_claim_ids"] == ["C1"]
    assert report["coverage_metrics"]["actual_covered_claim_count"] == 1


def test_gap_auditor_never_receives_raw_citation_markers(tmp_path: Path) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    old_path.write_text(
        "PINNs embed equations [REF:paper-s2-001] and recover data [1].",
        encoding="utf-8",
    )
    ledger = _ledger_for_fake_packet()
    caller, calls = _fake_caller(ledger)

    enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-auditor-sanitized",
        live=True,
        qwen_caller=caller,
    )

    auditor_message = next(
        call["messages"][-1]["content"]
        for call in calls
        if call["agent_name"] == "ChapterAssetLegacyGapAuditorAgent"
    )
    assert "[REF:" not in auditor_message
    assert "paper-s2-001" not in auditor_message
    assert "paper-ht-003" not in auditor_message
    assert "[CITATION]" in auditor_message


def test_patch_unknown_handle_is_preserved_without_integrity_marker(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    old_text = old_path.read_text(encoding="utf-8").strip()

    def caller(agent_name: str, *_args, **_kwargs):
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 60,
            "output_tokens": 30,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "Generated evidence-grounded prose.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {
                    "verdict": "gaps_found",
                    "gaps": [
                        {
                            "gap_id": "G01",
                            "old_draft_snippet": "old",
                            "scientific_content": "missing detail",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "affected_block_indices": [1],
                        }
                    ],
                    "notes": "",
                }
            )
        elif agent_name == "ChapterAssetLegacyGapPatchWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "Invalid patch.",
                    "used_evidence_handles": ["E99_UNKNOWN"],
                }
            )
        elif agent_name == "ChapterAssetBlockScientificReviewerAgent":
            content = json.dumps({"review_rows": [], "notes": ""})
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-patch-soft",
        live=True,
        qwen_caller=caller,
    )

    assert report["status"] == "enhanced"
    gap_audit = json.loads(
        (
            tmp_path
            / "out-patch-soft"
            / "LEGACY_GAP_AUDIT.json"
        ).read_text(encoding="utf-8")
    )
    assert gap_audit["patch_records"][0]["applied"] is True
    assert "integrity" not in json.dumps(gap_audit).lower()
    enhanced = (
        tmp_path / "out-patch-soft" / "ENHANCED_CHAPTER.md"
    ).read_text(encoding="utf-8")
    assert "Invalid patch." in enhanced
    assert "E99_UNKNOWN" not in enhanced
    assert old_text not in enhanced.replace("PINNs embed", "PINNs embed")


def test_fail_open_with_h1_old_draft_has_single_heading(tmp_path: Path) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    old_path.write_text(
        "# Physics-Informed Neural Networks\n\n"
        "PINNs embed governing equations in the loss.",
        encoding="utf-8",
    )

    def caller(*_args, **_kwargs):
        raise RuntimeError("transport down")

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-h1",
        live=True,
        qwen_caller=caller,
    )

    old_artifact = (
        tmp_path / "out-h1" / "OLD_CHAPTER.md"
    ).read_text(encoding="utf-8")
    assert old_artifact == old_path.read_text(encoding="utf-8").rstrip("\n") + "\n"
    enhanced = (
        tmp_path / "out-h1" / "ENHANCED_CHAPTER.md"
    ).read_text(encoding="utf-8")
    assert [line for line in enhanced.splitlines() if line.startswith("# ")] == [
        "# Physics-Informed Neural Networks"
    ]
    assert report["status"] == "fail_open_original"


def test_claim_components_are_compacted_structurally(tmp_path: Path) -> None:
    packet_data = _fake_packet_data()
    packet_data["claims"][0]["supported_components"] = [
        {
            "component_id": "c1.1",
            "statement": "PINNs embed governing equations as residual terms.",
            "support_assessment": "direct",
            "reason": "The source explicitly describes residual loss terms.",
        }
    ]
    packet_path = tmp_path / "input_packet.json"
    old_path = tmp_path / "SECTION_DRAFT_EN.md"
    packet_path.write_text(
        json.dumps(packet_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    old_path.write_text("Old draft.", encoding="utf-8")
    ledger = enhancer._build_handle_ledger(
        enhancer._rehydrate_packet(packet_data)
    )
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    planner_messages: list[str] = []

    def caller(agent_name: str, messages: list[dict], **_kwargs):
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 50,
            "output_tokens": 20,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            planner_messages.append(messages[-1]["content"])
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "A PINN embeds physics in the loss.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-components",
        live=True,
        qwen_caller=caller,
    )

    planner_message = planner_messages[0]
    assert '"component_id"' in planner_message
    assert '"support_assessment"' in planner_message
    assert '"reason"' in planner_message
    assert "'component_id'" not in planner_message
    assert '"claim_components"' not in planner_message


def test_default_model_tiers_keep_planned_assignment() -> None:
    assert enhancer.DEFAULT_PLANNER_TIER == "c_model"
    assert enhancer.DEFAULT_BLOCK_TIER == "c_model"
    assert enhancer.DEFAULT_PATCH_TIER == "c_model"
    assert enhancer.DEFAULT_AUDITOR_TIER == "c2_model"
    assert enhancer.DEFAULT_CONTRACT_REPAIR_TIER == "c2_model"
    assert enhancer.DEFAULT_EXPLANATORY_TIER == "c2_model"


def test_planner_context_includes_section_responsibility(
    tmp_path: Path,
) -> None:
    packet_data = _fake_packet_data()
    packet_data["section_contract"].update(
        {
            "must_cover": ["mechanism", "boundary conditions"],
            "unique_contribution": "A bounded PINN mechanism synthesis.",
            "transition_contract": {
                "transition_from_previous": "Previous chapter closed on data-driven models.",
                "transition_to_next": "Next chapter opens on applications.",
            },
            "user_question": "How do PINNs enforce governing equations?",
            "dynamic_axes": ["physics embedding", "data scarcity"],
            "sibling_section_outlines": ["S02: applications and benchmarks"],
            "do_not_cover": ["full benchmark survey"],
        }
    )
    packet_data["manuscript_context"] = {
        "summary": "Manuscript is mid-draft after S01.",
    }
    packet_path = tmp_path / "input_packet.json"
    old_path = tmp_path / "SECTION_DRAFT_EN.md"
    packet_path.write_text(
        json.dumps(packet_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    old_path.write_text("Old draft.", encoding="utf-8")
    ledger = enhancer._build_handle_ledger(
        enhancer._rehydrate_packet(packet_data)
    )
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    planner_messages: list[str] = []

    def caller(agent_name: str, messages: list[dict], **_kwargs):
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 60,
            "output_tokens": 30,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            planner_messages.append(messages[-1]["content"])
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "A PINN embeds physics in the loss.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-context",
        live=True,
        qwen_caller=caller,
    )

    message = planner_messages[0]
    for field in (
        "must_cover",
        "unique_contribution",
        "transition_handoff",
        "user_context",
        "sibling_outlines",
        "do_not_cover",
    ):
        assert f'"{field}"' in message


def test_gap_without_valid_evidence_handles_is_record_only(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    calls: list[str] = []

    def caller(agent_name: str, *_args, **_kwargs):
        calls.append(agent_name)
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 60,
            "output_tokens": 30,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "Generated prose.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {
                    "verdict": "gaps_found",
                    "gaps": [
                        {
                            "gap_id": "G01",
                            "old_draft_snippet": "old",
                            "scientific_content": "unsupported gap",
                            "claim_handles": [c1],
                            "evidence_handles": [],
                            "affected_block_indices": [1],
                        }
                    ],
                    "notes": "",
                }
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-no-gap-evidence",
        live=True,
        qwen_caller=caller,
    )

    assert report["status"] == "enhanced"
    assert "ChapterAssetLegacyGapPatchWriterAgent" not in calls
    gap_audit = json.loads(
        (
            tmp_path
            / "out-no-gap-evidence"
            / "LEGACY_GAP_AUDIT.json"
        ).read_text(encoding="utf-8")
    )
    assert gap_audit["patch_records"][0]["reason"] == (
        "no_gap_specific_evidence_handles"
    )


def test_patch_preserves_old_evidence_and_unions_gap_handle(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    e2 = ledger.evidence_by_claim["C1"][1].handle

    def caller(agent_name: str, *_args, **_kwargs):
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 60,
            "output_tokens": 30,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "Generated prose.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {
                    "verdict": "gaps_found",
                    "gaps": [
                        {
                            "gap_id": "G01",
                            "old_draft_snippet": "old",
                            "scientific_content": "missing detail",
                            "claim_handles": [c1],
                            "evidence_handles": [e2],
                            "affected_block_indices": [1],
                        }
                    ],
                    "notes": "",
                }
            )
        elif agent_name == "ChapterAssetLegacyGapPatchWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "Patched prose.",
                    "used_evidence_handles": [e2],
                }
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-additive-patch",
        live=True,
        qwen_caller=caller,
    )

    assert report["status"] == "enhanced"
    blocks = json.loads(
        (
            tmp_path
            / "out-additive-patch"
            / "EXPLANATION_BLOCKS.json"
        ).read_text(encoding="utf-8")
    )["blocks"]
    assert blocks[0]["evidence_handles"] == [e1, e2]
    assert set(blocks[0]["planned_claim_handles"]) == {c1}
    gap_audit = json.loads(
        (
            tmp_path
            / "out-additive-patch"
            / "LEGACY_GAP_AUDIT.json"
        ).read_text(encoding="utf-8")
    )
    assert gap_audit["patch_records"][0]["applied"] is True
    assert gap_audit["patch_records"][0]["used_evidence_handles"] == [e1, e2]


def test_patch_without_gap_specific_handle_is_preserved(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    e2 = ledger.evidence_by_claim["C1"][1].handle

    def caller(agent_name: str, *_args, **_kwargs):
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 60,
            "output_tokens": 30,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "Generated prose.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {
                    "verdict": "gaps_found",
                    "gaps": [
                        {
                            "gap_id": "G01",
                            "old_draft_snippet": "old",
                            "scientific_content": "missing detail",
                            "claim_handles": [c1],
                            "evidence_handles": [e2],
                            "affected_block_indices": [1],
                        }
                    ],
                    "notes": "",
                }
            )
        elif agent_name == "ChapterAssetLegacyGapPatchWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "Patch without the gap handle.",
                    "used_evidence_handles": [e1],
                }
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-missing-gap-handle",
        live=True,
        qwen_caller=caller,
    )

    assert report["status"] == "enhanced"
    gap_audit = json.loads(
        (
            tmp_path
            / "out-missing-gap-handle"
            / "LEGACY_GAP_AUDIT.json"
        ).read_text(encoding="utf-8")
    )
    assert gap_audit["patch_records"][0]["applied"] is True
    blocks = json.loads(
        (
            tmp_path
            / "out-missing-gap-handle"
            / "EXPLANATION_BLOCKS.json"
        ).read_text(encoding="utf-8")
    )["blocks"]
    assert blocks[0]["prose"] == "Patch without the gap handle."
    assert blocks[0]["evidence_handles"] == [e1]


def _explanatory_fake_caller(
    ledger: enhancer.HandleLedger,
    *,
    explanatory_rows: list[dict],
    auditor_gaps: list[dict] | None = None,
    reranker_scores: list[dict] | None = None,
):
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    calls: list[str] = []

    def caller(agent_name: str, *_args, **_kwargs):
        calls.append(agent_name)
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 80,
            "output_tokens": 40,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "A PINN embeds physics in the loss.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetBlockScientificReviewerAgent":
            content = json.dumps({"review_rows": [], "notes": ""})
        elif agent_name == "ChapterAssetExplanatoryCitationPlannerAgent":
            content = json.dumps({"explanatory_rows": explanatory_rows})
        elif agent_name == "ChapterAssetExplanatorySemanticRerankerAgent":
            if reranker_scores is None:
                raise AssertionError(
                    "reranker not configured in fake caller"
                )
            content = json.dumps({"semantic_scores": reranker_scores})
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {
                    "verdict": "gaps_found" if auditor_gaps else "no_actionable_gaps",
                    "gaps": auditor_gaps or [],
                    "notes": "",
                }
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    return caller, calls


def test_explanatory_citations_local_first_and_core_coverage_unchanged(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    caller, calls = _explanatory_fake_caller(
        ledger,
        explanatory_rows=[
            {
                "block_index": 1,
                "target_sentence": "A PINN embeds physics in the loss.",
                "benefit_type": "definition",
                "query": "physics informed neural network residual loss",
            }
        ],
    )
    s2_called = {"value": False}

    def local_search(query: str, max_results: int) -> list[dict]:
        return [
            {
                "paper_id": "local-paper-1",
                "title": "Local PINN Tutorial",
                "authors": ["A. Author"],
                "year": 2024,
                "doi": "10.1/local-pinn",
                "abstract": "Introduces residual-loss training for PINNs.",
                "venue": "Local Journal",
                "relevance_score": 0.9,
            }
        ]

    def s2_search(query: str, max_results: int) -> list[dict]:
        s2_called["value"] = True
        return []

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-explanatory-local",
        live=True,
        qwen_caller=caller,
        local_search_callback=local_search,
        s2_search_callback=s2_search,
    )

    assert s2_called["value"] is False
    assert "ChapterAssetExplanatoryCitationPlannerAgent" in calls
    ledger_data = json.loads(
        (
            tmp_path
            / "out-explanatory-local"
            / "EXPLANATORY_CITATION_LEDGER.json"
        ).read_text(encoding="utf-8")
    )
    assert len(ledger_data["records"]) == 1
    record = ledger_data["records"][0]
    assert record["role"] == "explanatory_context"
    assert record["permission"] == "background_explanation_only"
    assert record["retrieval_origin"] == "local_metadata"
    assert record["metadata"]["title"] == "Local PINN Tutorial"
    assert "selection_audit" in ledger_data
    assert ledger_data["selection_audit"]["selected_unique_count"] == 1
    enhanced = (
        tmp_path / "out-explanatory-local" / "ENHANCED_CHAPTER.md"
    ).read_text(encoding="utf-8")
    assert "[REF:doi:10.1/local-pinn]" in enhanced
    assert (
        "A PINN embeds physics in the loss. [REF:doi:10.1/local-pinn]"
        in enhanced
    )
    assert report["reference_metrics"]["core_reference_count"] == 1
    assert report["reference_metrics"]["explanatory_reference_count"] == 1
    assert report["reference_metrics"]["total_deduplicated_bibliography_count"] == 2
    assert report["coverage_metrics"]["actual_covered_claim_count"] == 1
    claim_map = json.loads(
        (
            tmp_path
            / "out-explanatory-local"
            / "CLAIM_TO_PARAGRAPH_MAP.json"
        ).read_text(encoding="utf-8")
    )
    assert claim_map["claim_to_paragraph"]["C1"] == [1]


def test_explanatory_citations_fall_back_to_s2_when_local_empty(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    caller, _calls = _explanatory_fake_caller(
        ledger,
        explanatory_rows=[
            {
                "block_index": 1,
                "target_sentence": "A PINN embeds physics in the loss.",
                "benefit_type": "mechanism_background",
                "query": "governing equation residual training",
            }
        ],
    )
    s2_called = {"value": False}

    def local_search(query: str, max_results: int) -> list[dict]:
        return []

    def s2_search(query: str, max_results: int) -> list[dict]:
        s2_called["value"] = True
        return [
            {
                "semantic_scholar_paper_id": "s2-abc",
                "title": "S2 Background Paper",
                "authors": ["B. Author"],
                "year": 2023,
                "abstract": "Background on residual losses.",
                "relevance_score": 0.8,
            }
        ]

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-explanatory-s2",
        live=True,
        qwen_caller=caller,
        local_search_callback=local_search,
        s2_search_callback=s2_search,
    )

    assert s2_called["value"] is True
    ledger_data = json.loads(
        (
            tmp_path
            / "out-explanatory-s2"
            / "EXPLANATORY_CITATION_LEDGER.json"
        ).read_text(encoding="utf-8")
    )
    assert ledger_data["records"][0]["retrieval_origin"] == "semantic_scholar"
    assert report["reference_metrics"]["explanatory_reference_count"] == 1


def test_explanatory_citations_deduplicate_across_blocks(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    c2 = ledger.claim_by_id["C2"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    e3 = ledger.evidence_by_claim["C2"][0].handle
    search_calls = {"count": 0}
    block_calls = {"count": 0}

    def caller(agent_name: str, *_args, **_kwargs):
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 80,
            "output_tokens": 40,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block one",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        },
                        {
                            "block_index": 2,
                            "title": "Block two",
                            "block_type": "scientific_consequence",
                            "goal": "Apply.",
                            "claim_handles": [c2],
                            "evidence_handles": [e3],
                            "omitted_handle_reasons": {},
                        },
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            block_calls["count"] += 1
            index = block_calls["count"]
            content = json.dumps(
                {
                    "paragraph_prose": f"Prose for block {index}.",
                    "used_evidence_handles": [e1 if index == 1 else e3],
                }
            )
        elif agent_name == "ChapterAssetExplanatoryCitationPlannerAgent":
            content = json.dumps(
                {
                    "explanatory_rows": [
                        {
                            "block_index": 1,
                            "target_sentence": "Prose for block 1.",
                            "benefit_type": "definition",
                            "query": "same concept",
                        },
                        {
                            "block_index": 2,
                            "target_sentence": "Prose for block 2.",
                            "benefit_type": "representative_application",
                            "query": "same concept",
                        },
                    ]
                }
            )
        elif agent_name == "ChapterAssetRepresentativeApplicationWriterAgent":
            payload = json.loads(_args[0][-1]["content"])
            content = json.dumps(
                {
                    "application_rows": [
                        {
                            "target_handle": row["target_handle"],
                            "prose": (
                                "One study applied the shared concept in "
                                "practice and reported a measured gain."
                            ),
                        }
                        for row in payload["targets"]
                    ]
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    def local_search(query: str, max_results: int) -> list[dict]:
        search_calls["count"] += 1
        return [
            {
                "paper_id": "shared-paper",
                "title": "Shared Explanatory Paper",
                "abstract": "Explains the shared concept.",
            }
        ]

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-explanatory-dedup",
        live=True,
        qwen_caller=caller,
        local_search_callback=local_search,
    )

    ledger_data = json.loads(
        (
            tmp_path
            / "out-explanatory-dedup"
            / "EXPLANATORY_CITATION_LEDGER.json"
        ).read_text(encoding="utf-8")
    )
    assert len(ledger_data["records"]) == 1
    assert set(ledger_data["records"][0]["target_blocks"]) == {1, 2}
    assert set(ledger_data["block_to_explanatory_handles"]) == {"1", "2"}
    assert report["reference_metrics"]["explanatory_reference_count"] == 1
    assert report["reference_metrics"]["total_deduplicated_bibliography_count"] == 3


def test_explanatory_search_failure_fails_open_with_empty_ledger(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    caller, _calls = _explanatory_fake_caller(
        ledger,
        explanatory_rows=[
            {
                "block_index": 1,
                "target_sentence": "A PINN embeds physics in the loss.",
                "benefit_type": "definition",
                "query": "PINN",
            }
        ],
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        raise RuntimeError("store unavailable")

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-explanatory-fail",
        live=True,
        qwen_caller=caller,
        local_search_callback=local_search,
    )

    assert report["status"] == "enhanced"
    ledger_data = json.loads(
        (
            tmp_path
            / "out-explanatory-fail"
            / "EXPLANATORY_CITATION_LEDGER.json"
        ).read_text(encoding="utf-8")
    )
    assert ledger_data["records"] == []
    assert any("local_metadata_search_error" in d for d in ledger_data["diagnostics"])


def test_cli_exposes_optional_explanatory_controls() -> None:
    parser = cli.build_arg_parser()
    args = parser.parse_args(
        [
            "--packet-path",
            "packet.json",
            "--old-draft",
            "old.md",
            "--output-dir",
            "out",
            "--local-metadata-store",
            "local.sqlite",
            "--s2-search",
            "--explanatory-max-results",
            "7",
        ]
    )
    assert args.local_metadata_store is not None
    assert args.s2_search is True
    assert args.explanatory_max_results == 7


def test_real_s05_nested_context_fields_reach_planner(
    tmp_path: Path,
) -> None:
    packet_data = _fake_packet_data()
    packet_data["manuscript_context"] = {
        "research_context": {
            "user_question": "How do PINNs enforce governing equations?",
            "global_review_thesis": "Physics embedding is the central review thesis.",
            "global_narrative_strategy": "Build from mechanism to applications.",
            "provisional_must_cover": ["provisional mechanism coverage"],
            "provisional_unique_contribution": "Provisional contribution value.",
            "provisional_argument_role": "Provisional argument role.",
            "provisional_key_questions": ["Provisional key question."],
        },
        "current_section_boundary_contract": {
            "assigned_user_axes": ["axis-physics", "axis-data"],
            "must_not_cover": ["full benchmark survey"],
            "must_cover": ["boundary mechanism coverage"],
            "unique_contribution": "Boundary contribution value.",
            "argument_role": "Boundary argument role.",
            "key_questions": ["Boundary key question."],
            "handoff_from_previous": "Previous chapter closed on data-driven models.",
            "handoff_to_next": "Next chapter opens on heat-transfer applications.",
        },
        "full_section_workplan": [
            {"section_id": "S02", "title": "Applications", "goal": "Apply PINNs."}
        ],
        "sibling_section_responsibilities": ["S03 owns benchmarks."],
    }
    packet_data["section_contract"].update(
        {
            "assigned_user_axes": ["axis-physics", "axis-data"],
            "argument_role": "",
            "unique_contribution": "",
            "key_questions": [],
            "must_cover": [],
        }
    )
    packet_path = tmp_path / "input_packet.json"
    old_path = tmp_path / "SECTION_DRAFT_EN.md"
    packet_path.write_text(
        json.dumps(packet_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    old_path.write_text("Old draft.", encoding="utf-8")
    ledger = enhancer._build_handle_ledger(
        enhancer._rehydrate_packet(packet_data)
    )
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    planner_messages: list[str] = []

    def caller(agent_name: str, messages: list[dict], **_kwargs):
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 60,
            "output_tokens": 30,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            planner_messages.append(messages[-1]["content"])
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "A PINN embeds physics in the loss.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-s05-context",
        live=True,
        qwen_caller=caller,
    )

    message = planner_messages[0]
    for value in (
        "How do PINNs enforce governing equations?",
        "axis-physics",
        "full benchmark survey",
        "boundary mechanism coverage",
        "Boundary contribution value.",
        "Boundary argument role.",
        "Boundary key question.",
        "Previous chapter closed on data-driven models.",
        "Next chapter opens on heat-transfer applications.",
        "Applications",
        "Physics embedding is the central review thesis.",
        "Build from mechanism to applications.",
        "Boundary key question.",
    ):
        assert value in message


def _selection_record(handle: str, score: float) -> dict:
    return {
        "handle": handle,
        "selection_score": score,
        "target_blocks": [],
        "target_sentences": [],
        "queries": [],
        "targets": [],
        "benefit_types": [],
    }


def test_adaptive_selection_rich_pool_lands_in_soft_range() -> None:
    records = [
        _selection_record(f"R{i:02d}", 1.0 - index * 0.01)
        for index, i in enumerate(range(15))
    ]
    kept, audit = enhancer._apply_global_selection(
        records,
        raw_unique_count=25,
        eligible_unique_count=20,
    )
    assert len(kept) == 15
    assert audit["stop_reason"] == "within_soft_range"
    assert audit["selected_score_floor"] == 0.86


def test_adaptive_selection_weak_pool_is_not_padded() -> None:
    records = [
        _selection_record("R01", 0.8),
        _selection_record("R02", 0.75),
        _selection_record("R03", 0.7),
    ]
    kept, audit = enhancer._apply_global_selection(
        records,
        raw_unique_count=3,
        eligible_unique_count=3,
    )
    assert len(kept) == 3
    assert audit["stop_reason"] == "below_soft_min_but_not_padded"


def test_adaptive_selection_high_quality_distinct_needs_not_count_capped() -> None:
    records = [
        _selection_record(f"R{i:02d}", 0.9) for i in range(25)
    ]
    kept, audit = enhancer._apply_global_selection(
        records,
        raw_unique_count=30,
        eligible_unique_count=28,
    )
    assert len(kept) == 25
    assert audit["stop_reason"] == "above_soft_max_high_quality_margins"


def test_per_need_selection_keeps_at_most_two_close_sources() -> None:
    candidates = [
        {
            "stable_paper_id": f"p{index}",
            "marker_id": f"p{index}",
            "metadata": {
                "title": (
                    "PINN physics residual loss"
                    if index < 2
                    else "Unrelated chemistry"
                ),
                "abstract": (
                    "PINN residual loss training"
                    if index < 2
                    else "chemistry unrelated"
                ),
            },
            "relevance_score": 1.0 if index == 0 else 0.9 if index == 1 else 0.0,
            "relevance_reason": "",
            "origin": "local_metadata",
        }
        for index in range(10)
    ]
    selected = enhancer._select_per_need(
        candidates, query="PINN residual loss"
    )
    assert len(selected) == 2


def test_local_below_floor_triggers_s2_fallback(tmp_path: Path) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    caller, _calls = _explanatory_fake_caller(
        ledger,
        explanatory_rows=[
            {
                "block_index": 1,
                "target_sentence": "A PINN embeds physics in the loss.",
                "benefit_type": "definition",
                "query": "PINN residual loss",
            }
        ],
    )
    s2_called = {"value": False}

    def local_search(query: str, max_results: int) -> list[dict]:
        return [
            {
                "paper_id": "local-unrelated",
                "title": "Unrelated Chemistry",
                "abstract": "Chemical synthesis unrelated to PINNs.",
            }
        ]

    def s2_search(query: str, max_results: int) -> list[dict]:
        s2_called["value"] = True
        return [
            {
                "semantic_scholar_paper_id": "s2-good",
                "title": "PINN Residual Loss Background",
                "abstract": "Explains PINN residual loss training.",
                "relevance_score": 0.9,
            }
        ]

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-local-below-floor",
        live=True,
        qwen_caller=caller,
        local_search_callback=local_search,
        s2_search_callback=s2_search,
    )

    assert s2_called["value"] is True
    ledger_data = json.loads(
        (
            tmp_path
            / "out-local-below-floor"
            / "EXPLANATORY_CITATION_LEDGER.json"
        ).read_text(encoding="utf-8")
    )
    assert ledger_data["records"][0]["retrieval_origin"] == "semantic_scholar"
    assert report["reference_metrics"]["explanatory_reference_count"] == 1


def test_sentence_not_found_falls_back_to_block_level_marker(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    caller, _calls = _explanatory_fake_caller(
        ledger,
        explanatory_rows=[
            {
                "block_index": 1,
                "target_sentence": "This exact sentence is missing.",
                "benefit_type": "definition",
                "query": "PINN residual loss",
            }
        ],
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return [
            {
                "paper_id": "fallback-paper",
                "title": "Fallback Explanatory Paper",
                "abstract": "PINN residual loss background.",
                "relevance_score": 0.8,
            }
        ]

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-sentence-fallback",
        live=True,
        qwen_caller=caller,
        local_search_callback=local_search,
    )

    ledger_data = json.loads(
        (
            tmp_path
            / "out-sentence-fallback"
            / "EXPLANATORY_CITATION_LEDGER.json"
        ).read_text(encoding="utf-8")
    )
    assert any(
        "sentence_not_found_block_level_fallback" in diagnostic
        for diagnostic in ledger_data["diagnostics"]
    )
    enhanced = (
        tmp_path / "out-sentence-fallback" / "ENHANCED_CHAPTER.md"
    ).read_text(encoding="utf-8")
    assert "[REF:fallback-paper]" in enhanced
    assert report["reference_metrics"]["explanatory_reference_count"] == 1


def test_doi_stable_identity_normalization_and_preference() -> None:
    first = enhancer._stable_paper_identity(
        {
            "paper_id": "internal-1",
            "doi": "https://doi.org/10.1000/ABC",
            "title": "Same Paper",
        }
    )
    second = enhancer._stable_paper_identity(
        {
            "semantic_scholar_paper_id": "s2-other",
            "doi": "doi:10.1000/abc",
            "title": "Same Paper",
        }
    )
    assert first == ("doi:10.1000/abc", "doi:10.1000/abc")
    assert second == first


def test_cli_local_metadata_callback_is_read_only_for_papers(
    tmp_path: Path,
) -> None:
    terms = cli._expand_local_query_terms(
        "How do physics informed neural networks enforce residual loss?"
    )
    assert len(terms) <= 8
    assert "physics informed" in terms
    assert "residual" in terms

    store_path = tmp_path / "existing.sqlite"
    conn = sqlite3.connect(str(store_path))
    try:
        conn.execute(
            """
            CREATE TABLE papers (
                paper_id TEXT, doi TEXT, title TEXT, year INTEGER,
                venue TEXT, search_text TEXT, raw_json TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO papers VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "paper-readonly",
                "10.1000/readonly",
                "Readonly Metadata Paper",
                2024,
                "Test Venue",
                "readonly metadata search",
                json.dumps({"authors": ["Alice", "Bob"], "abstract": "Readonly."}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    before = store_path.read_bytes()
    callback = cli._LocalMetadataCallback(store_path)
    rows = callback("readonly metadata", 5)
    callback("readonly metadata", 5)
    callback.close()
    after = store_path.read_bytes()
    tables = [
        row[0]
        for row in sqlite3.connect(store_path).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    ]
    sqlite3.connect(store_path).close()

    assert len(rows) == 1
    assert rows[0]["paper_id"] == "paper-readonly"
    assert after == before
    assert tables == ["papers"]


def test_explanatory_sentence_handle_resolves_locally(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    caller, _calls = _explanatory_fake_caller(
        ledger,
        explanatory_rows=[
            {
                "sentence_handle": "B01-S01",
                "benefit_type": "definition",
                "query": "PINN residual loss",
            }
        ],
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return [
            {
                "paper_id": "handle-paper",
                "title": "Handle Paper",
                "abstract": "PINN residual loss background.",
                "relevance_score": 0.8,
            }
        ]

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-sentence-handle",
        live=True,
        qwen_caller=caller,
        local_search_callback=local_search,
    )

    ledger_data = json.loads(
        (
            tmp_path
            / "out-sentence-handle"
            / "EXPLANATORY_CITATION_LEDGER.json"
        ).read_text(encoding="utf-8")
    )
    assert ledger_data["records"][0]["target_sentences"] == [
        "A PINN embeds physics in the loss."
    ]
    enhanced = (
        tmp_path / "out-sentence-handle" / "ENHANCED_CHAPTER.md"
    ).read_text(encoding="utf-8")
    assert (
        "A PINN embeds physics in the loss. [REF:handle-paper]"
        in enhanced
    )
    assert report["reference_metrics"]["explanatory_reference_count"] == 1


def test_invalid_sentence_handle_is_diagnostic_and_skipped(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    caller, _calls = _explanatory_fake_caller(
        ledger,
        explanatory_rows=[
            {
                "sentence_handle": "B99-S99",
                "benefit_type": "definition",
                "query": "PINN",
            }
        ],
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return [
            {
                "paper_id": "paper",
                "title": "Paper",
                "abstract": "PINN residual loss.",
            }
        ]

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-invalid-handle",
        live=True,
        qwen_caller=caller,
        local_search_callback=local_search,
    )

    ledger_data = json.loads(
        (
            tmp_path
            / "out-invalid-handle"
            / "EXPLANATORY_CITATION_LEDGER.json"
        ).read_text(encoding="utf-8")
    )
    assert ledger_data["records"] == []
    assert any(
        "invalid sentence_handle" in diagnostic
        for diagnostic in ledger_data["diagnostics"]
    )
    assert report["status"] == "enhanced"


def test_patch_before_explanatory_anchors_patched_sentence(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle

    def caller(agent_name: str, *_args, **_kwargs):
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 80,
            "output_tokens": 40,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "A PINN embeds physics in the loss.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetExplanatoryCitationPlannerAgent":
            content = json.dumps(
                {
                    "explanatory_rows": [
                        {
                            "sentence_handle": "B01-S01",
                            "benefit_type": "definition",
                            "query": "PINN residual loss",
                        }
                    ]
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {
                    "verdict": "gaps_found",
                    "gaps": [
                        {
                            "gap_id": "G01",
                            "old_draft_snippet": "old",
                            "scientific_content": "missing detail",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "affected_block_indices": [1],
                        }
                    ],
                    "notes": "",
                }
            )
        elif agent_name == "ChapterAssetLegacyGapPatchWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": (
                        "Patched prose no longer contains the original "
                        "sentence."
                    ),
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetBlockScientificReviewerAgent":
            content = json.dumps({"review_rows": [], "notes": ""})
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    def local_search(query: str, max_results: int) -> list[dict]:
        return [
            {
                "paper_id": "anchor-paper",
                "title": "Anchor Paper",
                "abstract": "PINN residual loss background.",
                "relevance_score": 0.8,
            }
        ]

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-anchor-reconcile",
        live=True,
        qwen_caller=caller,
        local_search_callback=local_search,
    )

    ledger_data = json.loads(
        (
            tmp_path
            / "out-anchor-reconcile"
            / "EXPLANATORY_CITATION_LEDGER.json"
        ).read_text(encoding="utf-8")
    )
    assert not any(
        "explanatory_anchor_moved_to_block_end" in diagnostic
        for diagnostic in ledger_data["diagnostics"]
    )
    enhanced = (
        tmp_path / "out-anchor-reconcile" / "ENHANCED_CHAPTER.md"
    ).read_text(encoding="utf-8")
    assert "[REF:anchor-paper]" in enhanced
    assert "A PINN embeds physics in the loss." not in enhanced
    assert (
        "Patched prose no longer contains the original sentence. "
        "[REF:anchor-paper]" in enhanced
    )
    assert report["reference_metrics"]["explanatory_reference_count"] == 1
    assert enhanced.count("[REF:anchor-paper]") == 1


def test_s2_normalization_accepts_backend_field_names() -> None:
    normalized = enhancer._normalize_explanatory_source(
        {
            "url_or_doi": "https://doi.org/10.9/XYZ",
            "abstract_or_snippet": "S2 abstract snippet.",
            "journal_or_venue": "S2 Journal",
            "source_url": "https://source.example/paper",
            "title": "S2 Paper",
        },
        origin="semantic_scholar",
    )
    assert normalized is not None
    assert normalized["stable_paper_id"] == "doi:10.9/xyz"
    assert normalized["metadata"]["abstract"] == "S2 abstract snippet."
    assert normalized["metadata"]["venue"] == "S2 Journal"
    assert normalized["metadata"]["url"] == "https://source.example/paper"


def test_research_context_provisional_fallback_when_boundary_blank() -> None:
    packet_data = _fake_packet_data()
    packet_data["section_contract"] = {"title": "Fallback Section"}
    packet_data["manuscript_context"] = {
        "research_context": {
            "provisional_must_cover": ["provisional coverage"],
            "provisional_unique_contribution": "Provisional contribution.",
            "provisional_argument_role": "Provisional role.",
            "provisional_key_questions": ["Provisional question."],
        }
    }
    packet = enhancer._rehydrate_packet(packet_data)
    ledger = enhancer._build_handle_ledger(packet)
    context = enhancer._build_compact_context(packet, ledger)
    responsibility = context["section_responsibility"]
    assert responsibility["must_cover"] == ["provisional coverage"]
    assert responsibility["unique_contribution"] == "Provisional contribution."
    assert responsibility["argument_role"] == "Provisional role."
    assert responsibility["key_questions"] == ["Provisional question."]


def test_generic_local_overlap_uses_s2_and_selects_best_combined(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    caller, _calls = _explanatory_fake_caller(
        ledger,
        explanatory_rows=[
            {
                "sentence_handle": "B01-S01",
                "benefit_type": "mechanism_background",
                "query": "hPINN implicit regularization",
            }
        ],
    )
    s2_called = {"value": False}

    def local_search(query: str, max_results: int) -> list[dict]:
        return [
            {
                "paper_id": "generic-local",
                "title": "Adaptive framework design",
                "abstract": (
                    "adaptive gradient neural network framework design "
                    "algorithm"
                ),
                "relevance_score": 0.5,
            }
        ]

    def s2_search(query: str, max_results: int) -> list[dict]:
        s2_called["value"] = True
        return [
            {
                "semantic_scholar_paper_id": "s2-hpinn",
                "title": "hPINN implicit regularization",
                "abstract": (
                    "hPINN implicit regularization stabilizes physics "
                    "informed residual training."
                ),
                "relevance_score": 0.9,
            }
        ]

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-generic-local",
        live=True,
        qwen_caller=caller,
        local_search_callback=local_search,
        s2_search_callback=s2_search,
    )

    assert s2_called["value"] is True
    ledger_data = json.loads(
        (
            tmp_path
            / "out-generic-local"
            / "EXPLANATORY_CITATION_LEDGER.json"
        ).read_text(encoding="utf-8")
    )
    assert ledger_data["records"][0]["retrieval_origin"] == "semantic_scholar"
    assert ledger_data["records"][0]["metadata"]["title"] == (
        "hPINN implicit regularization"
    )
    audit = ledger_data["selection_audit"]
    assert audit["route_counts"].get("local_plus_s2", 0) >= 1
    assert report["reference_metrics"]["explanatory_reference_count"] == 1


def test_technical_phrase_local_candidate_avoids_s2(tmp_path: Path) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    caller, _calls = _explanatory_fake_caller(
        ledger,
        explanatory_rows=[
            {
                "sentence_handle": "B01-S01",
                "benefit_type": "mechanism_background",
                "query": "hPINN implicit regularization",
            }
        ],
    )
    s2_called = {"value": False}

    def local_search(query: str, max_results: int) -> list[dict]:
        return [
            {
                "paper_id": "local-hpinn",
                "title": "hPINN implicit regularization",
                "abstract": (
                    "hPINN implicit regularization improves residual "
                    "physics training."
                ),
                "relevance_score": 0.6,
            }
        ]

    def s2_search(query: str, max_results: int) -> list[dict]:
        s2_called["value"] = True
        return []

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-technical-local",
        live=True,
        qwen_caller=caller,
        local_search_callback=local_search,
        s2_search_callback=s2_search,
    )

    assert s2_called["value"] is False
    ledger_data = json.loads(
        (
            tmp_path
            / "out-technical-local"
            / "EXPLANATORY_CITATION_LEDGER.json"
        ).read_text(encoding="utf-8")
    )
    assert ledger_data["records"][0]["retrieval_origin"] == "local_metadata"
    assert ledger_data["selection_audit"]["route_counts"]["local_only"] >= 1
    assert report["reference_metrics"]["explanatory_reference_count"] == 1


def test_planner_prompt_puts_section_responsibility_first() -> None:
    prompt = enhancer._read_prompt(enhancer.PROMPT_PLANNER)
    assert "Section responsibility is the first priority" in prompt
    assert "must_cover" in prompt
    assert "evidence_gap" in prompt
    assert "responsibility_coverage_rows" in prompt


def test_responsibility_coverage_rows_persisted_when_clean(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle

    def caller(agent_name: str, *_args, **_kwargs):
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 60,
            "output_tokens": 30,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "responsibility_coverage_rows": [
                        {
                            "responsibility": "surrogate-assisted robust design",
                            "status": "covered",
                            "claim_handles": [c1],
                            "note": "Covered in block 1.",
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "A PINN embeds physics in the loss.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-responsibility-rows",
        live=True,
        qwen_caller=caller,
    )

    plan = json.loads(
        (
            tmp_path
            / "out-responsibility-rows"
            / "CHAPTER_ARGUMENT_PLAN.json"
        ).read_text(encoding="utf-8")
    )["plan"]
    assert plan["responsibility_coverage_rows"][0]["responsibility"] == (
        "surrogate-assisted robust design"
    )
    assert plan["responsibility_coverage_rows"][0]["status"] == "covered"


def test_local_query_expansion_avoids_generic_singletons() -> None:
    terms = cli._expand_local_query_terms(
        "adaptive framework design algorithm model"
    )
    assert "adaptive" not in terms
    assert "framework" not in terms
    assert "model" not in terms
    assert len(terms) <= 7


def test_call_model_uses_bounded_retry_and_key_rotation() -> None:
    captured: dict[str, object] = {}

    def caller(agent_name, messages, **kwargs):
        captured.update(kwargs)
        return {
            "content": "{}",
            "_llm_usage": {
                "model_name": "qwen3.7-flash",
                "input_tokens": 3,
                "output_tokens": 1,
                "success": True,
            },
        }

    enhancer._call_model(
        agent_name="ChapterAssetArgumentPlannerAgent",
        prompt_path=enhancer.PROMPT_PLANNER,
        payload={"section_id": "S01"},
        model_tier="c2_model",
        qwen_caller=caller,
        usage_records=[],
    )

    assert captured["max_retries"] == 2
    assert captured["max_transport_key_candidates"] == 2
    assert captured["allow_model_fallback"] is False


def test_usage_diagnostics_are_sanitized_and_persisted() -> None:
    usage_records: list[dict] = []
    enhancer._collect_usage(
        agent_name="ChapterAssetArgumentPlannerAgent",
        model_tier="c2_model",
        result={
            "content": "[fallback] Qwen chat failed: URLError.",
            "_llm_usage": {
                "model_name": "qwen3.7-flash",
                "input_tokens": 1,
                "output_tokens": 1,
                "success": False,
                "error_type": "URLError",
                "fallback_used": True,
                "model_fallback_used": False,
                "attempted_models": ["qwen3.7-flash"],
                "request_attempt_count": 3,
                "retry_count": 2,
                "api_key_masked": "masked-secret",
                "key_failures": ["masked-secret:URLError"],
            },
        },
        messages=[{"role": "user", "content": "test"}],
        usage_records=usage_records,
    )

    record = usage_records[0]
    assert record["error"] == "URLError"
    assert record["fallback_used"] is True
    assert record["attempted_models"] == ["qwen3.7-flash"]
    assert record["request_attempt_count"] == 3
    assert record["retry_count"] == 2
    assert "api_key_masked" not in record
    assert "key_failures" not in record


def test_cli_local_metadata_callback_falls_back_to_papers(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "local.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE papers (
                paper_id TEXT,
                doi TEXT,
                title TEXT,
                year INTEGER,
                venue TEXT,
                search_text TEXT,
                raw_json TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO papers (
                paper_id, doi, title, year, venue, search_text, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "paper-bic",
                "10.1000/bic",
                "Quasi-BIC Metasurface Review",
                2024,
                "Test Venue",
                "quasi bound states in the continuum quality factor",
                json.dumps(
                    {
                        "authors": [
                            {"name": "Alice Example"},
                            "Bob Example",
                        ],
                        "abstract": "Local explanatory abstract.",
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    callback = cli._LocalMetadataCallback(db_path)
    try:
        rows = callback("quasi-BIC quality factor", 5)
    finally:
        callback.close()

    assert len(rows) == 1
    assert rows[0]["paper_id"] == "paper-bic"
    assert rows[0]["authors"] == ["Alice Example", "Bob Example"]
    assert rows[0]["abstract"] == "Local explanatory abstract."


def test_cli_local_metadata_callback_is_read_only_for_abstract_papers(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "abstract.sqlite"
    conn = sqlite3.connect(str(store_path))
    try:
        conn.execute(
            """
            CREATE TABLE abstract_papers (
                paper_id TEXT,
                doi TEXT,
                semantic_scholar_id TEXT,
                openalex_id TEXT,
                title TEXT,
                title_identity TEXT,
                authors_json TEXT,
                year INTEGER,
                venue TEXT,
                abstract TEXT,
                citation_count INTEGER,
                open_access INTEGER,
                pdf_url TEXT,
                landing_page_url TEXT,
                source_apis_json TEXT,
                query_used_json TEXT,
                matched_keywords_json TEXT,
                topic_tags_json TEXT,
                embedding_id TEXT,
                raw_json TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO abstract_papers (
                paper_id, doi, semantic_scholar_id, openalex_id, title,
                authors_json, year, venue, abstract, raw_json,
                source_apis_json, query_used_json, matched_keywords_json,
                topic_tags_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "paper-abstract-readonly",
                "10.1000/abstract-readonly",
                "s2-1",
                "oa-1",
                "Abstract Readonly Paper",
                json.dumps(["Alice Example", "Bob Example"]),
                2025,
                "Test Venue",
                "Readonly abstract metadata.",
                json.dumps({"source_audit": {"status": "accepted"}}),
                json.dumps(["s2"]),
                json.dumps(["readonly"]),
                json.dumps(["metadata"]),
                json.dumps(["review"]),
                "2025-01-01T00:00:00",
                "2025-01-01T00:00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    before = store_path.read_bytes()
    callback = cli._LocalMetadataCallback(store_path)
    rows = callback("readonly metadata", 5)
    callback("readonly metadata", 5)
    callback.close()
    after = store_path.read_bytes()

    assert len(rows) == 1
    assert rows[0]["paper_id"] == "paper-abstract-readonly"
    assert rows[0]["authors"] == ["Alice Example", "Bob Example"]
    assert after == before


def test_evidence_gap_only_claim_is_plannable_without_evidence() -> None:
    ledger = _ledger_for_fake_packet()
    gap_handle = ledger.claim_by_id["C3"].handle
    plan = {
        "chapter_thesis": "Thesis.",
        "reader_takeaway": "Takeaway.",
        "argument_sequence": [
            {
                "step_index": 1,
                "purpose": "State the open comparison.",
                "claim_handles": [gap_handle],
                "evidence_handles": [],
            }
        ],
        "terminology_rows": [],
        "explanation_block_rows": [
            {
                "block_index": 1,
                "title": "Open comparison",
                "block_type": "explanatory_body",
                "goal": "State the open comparison without asserting it.",
                "claim_handles": [gap_handle],
                "evidence_handles": [],
                "omitted_handle_reasons": {},
            }
        ],
        "omitted_handle_reasons": {},
    }

    normalized, warnings = enhancer._normalize_plan(plan, ledger)

    assert normalized["explanation_block_rows"][0]["claim_handles"] == [
        gap_handle
    ]
    assert normalized["explanation_block_rows"][0]["evidence_handles"] == []
    assert not any(
        f"usable claim not assigned or omitted: {gap_handle}" in warning
        for warning in warnings
    )
    assert not enhancer._claim_requires_core_evidence(
        ledger.claim(gap_handle)
    )


def test_factual_or_hedged_claim_without_evidence_is_tolerated() -> None:
    ledger = _ledger_for_fake_packet()
    factual_handle = ledger.claim_by_id["C1"].handle
    plan = {
        "chapter_thesis": "Thesis.",
        "reader_takeaway": "Takeaway.",
        "argument_sequence": [],
        "terminology_rows": [],
        "explanation_block_rows": [
            {
                "block_index": 1,
                "title": "Unsupported factual block",
                "block_type": "explanatory_body",
                "goal": "Write a factual mechanism block.",
                "claim_handles": [factual_handle],
                "evidence_handles": [],
                "omitted_handle_reasons": {},
            }
        ],
        "omitted_handle_reasons": {},
    }

    normalized, _warnings = enhancer._normalize_plan(plan, ledger)
    assert normalized["explanation_block_rows"][0]["claim_handles"] == [
        factual_handle
    ]
    assert normalized["explanation_block_rows"][0]["evidence_handles"] == []


def test_unknown_and_cross_claim_handles_are_preserved_without_warnings() -> None:
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e2 = ledger.evidence_by_claim["C2"][0].handle
    plan = {
        "chapter_thesis": "Thesis.",
        "reader_takeaway": "Takeaway.",
        "argument_sequence": [
            {
                "step_index": 1,
                "purpose": "Preserve the model plan.",
                "claim_handles": ["C_UNKNOWN"],
                "evidence_handles": ["E_UNKNOWN"],
            }
        ],
        "terminology_rows": [],
        "explanation_block_rows": [
            {
                "block_index": 1,
                "title": "Tolerant carrier",
                "block_type": "explanatory_body",
                "goal": "Keep handles for downstream correction.",
                "claim_handles": [c1, "C_UNKNOWN"],
                "evidence_handles": [e2, "E_UNKNOWN"],
                "omitted_handle_reasons": {},
            }
        ],
        "omitted_handle_reasons": {"C_OMITTED_UNKNOWN": "not selected"},
    }

    normalized, warnings = enhancer._normalize_plan(plan, ledger)

    block = normalized["explanation_block_rows"][0]
    assert block["claim_handles"] == [c1, "C_UNKNOWN"]
    assert block["evidence_handles"] == [e2, "E_UNKNOWN"]
    assert normalized["argument_sequence"][0]["claim_handles"] == [
        "C_UNKNOWN"
    ]
    assert normalized["argument_sequence"][0]["evidence_handles"] == [
        "E_UNKNOWN"
    ]
    assert not any("unknown" in warning.lower() for warning in warnings)


def test_empty_block_with_available_gap_handle_is_tolerated() -> None:
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    c2 = ledger.claim_by_id["C2"].handle
    gap_handle = ledger.claim_by_id["C3"].handle
    plan = {
        "chapter_thesis": "Thesis.",
        "reader_takeaway": "Takeaway.",
        "argument_sequence": [],
        "terminology_rows": [],
        "explanation_block_rows": [
            {
                "block_index": 1,
                "title": "Empty carrier",
                "block_type": "explanatory_body",
                "goal": "Carry the unresolved comparison.",
                "claim_handles": [],
                "evidence_handles": [],
                "omitted_handle_reasons": {},
            }
        ],
        "omitted_handle_reasons": {
            c1: "covered by factual block",
            c2: "covered by hedged block",
        },
    }

    normalized, _warnings = enhancer._normalize_plan(plan, ledger)
    assert normalized["explanation_block_rows"][0]["claim_handles"] == []
    assert normalized["explanation_block_rows"][0]["evidence_handles"] == []


def test_evidence_permission_violation_blocking_requires_gap_claim() -> None:
    ledger = _ledger_for_fake_packet()
    factual_handle = ledger.claim_by_id["C1"].handle
    gap_handle = ledger.claim_by_id["C3"].handle
    blocks = [
        {
            "block_index": 1,
            "title": "Factual block",
            "block_type": "explanatory_body",
            "goal": "Factual evidence block.",
            "prose": "The reviewed work supports the mechanism.",
            "planned_claim_handles": [factual_handle],
            "claim_handles": [factual_handle],
            "evidence_handles": [ledger.evidence_by_claim["C1"][0].handle],
        },
        {
            "block_index": 2,
            "title": "Gap block",
            "block_type": "explanatory_body",
            "goal": "Open comparison.",
            "prose": "The evidence establishes superiority.",
            "planned_claim_handles": [gap_handle],
            "claim_handles": [gap_handle],
            "evidence_handles": [],
        },
    ]
    plan = {
        "section_id": "S01",
        "title": "Title",
        "chapter_thesis": "Thesis.",
        "reader_takeaway": "Takeaway.",
        "explanation_block_rows": [
            {
                "block_index": 1,
                "claim_handles": [factual_handle],
                "evidence_handles": [ledger.evidence_by_claim["C1"][0].handle],
            },
            {
                "block_index": 2,
                "claim_handles": [gap_handle],
                "evidence_handles": [],
            },
        ],
    }
    output = {
        "review_rows": [
            {
                "block_index": 1,
                "sentence": "The reviewed work supports the mechanism.",
                "flag_type": "evidence_permission_violation",
                "blocking": True,
                "issue": "factual block has no gap claim",
                "suggested_hedge": "",
                "claim_handles": [factual_handle],
                "evidence_handles": [ledger.evidence_by_claim["C1"][0].handle],
            },
            {
                "block_index": 2,
                "sentence": "The evidence establishes superiority.",
                "flag_type": "evidence_permission_violation",
                "blocking": True,
                "issue": "gap claim converted to fact",
                "suggested_hedge": "The evidence leaves superiority unresolved.",
                "claim_handles": [gap_handle],
                "evidence_handles": [],
            },
        ],
        "notes": "",
    }

    rows, warnings = enhancer._normalize_block_review_rows(
        output,
        blocks=blocks,
        ledger=ledger,
        plan=plan,
    )

    assert warnings == []
    assert rows[0]["blocking"] is False
    assert rows[1]["blocking"] is True


def test_evidence_permission_violation_triggers_one_revision(
    tmp_path: Path,
) -> None:
    packet = enhancer._rehydrate_packet(_fake_packet_data())
    ledger = enhancer._build_handle_ledger(packet)
    gap_handle = ledger.claim_by_id["C3"].handle
    plan = {
        "section_id": "S01",
        "title": "Title",
        "chapter_thesis": "Thesis.",
        "reader_takeaway": "Takeaway.",
        "argument_sequence": [],
        "terminology_rows": [],
        "explanation_block_rows": [
            {
                "block_index": 1,
                "title": "Open comparison",
                "block_type": "explanatory_body",
                "goal": "State the open comparison.",
                "claim_handles": [gap_handle],
                "evidence_handles": [],
                "omitted_handle_reasons": {},
            }
        ],
        "omitted_handle_reasons": {},
    }
    blocks = [
        {
            "block_index": 1,
            "title": "Open comparison",
            "block_type": "explanatory_body",
            "goal": "State the open comparison.",
            "prose": "PINNs outperform all classical solvers.",
            "planned_claim_handles": [gap_handle],
            "claim_handles": [gap_handle],
            "evidence_handles": [],
            "covered_claim_handles": [],
            "covered_claim_ids": [],
        }
    ]
    calls: list[str] = []

    def caller(agent_name: str, messages: list[dict], **_kwargs):
        calls.append(agent_name)
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 10,
            "output_tokens": 5,
        }
        if agent_name == "ChapterAssetBlockScientificReviewerAgent":
            content = json.dumps(
                {
                    "review_rows": [
                        {
                            "block_index": 1,
                            "sentence": "PINNs outperform all classical solvers.",
                            "flag_type": "evidence_permission_violation",
                            "blocking": True,
                            "issue": (
                                "Evidence-gap claim was asserted as fact."
                            ),
                            "suggested_hedge": (
                                "The reviewed material leaves open whether "
                                "PINNs outperform all classical solvers."
                            ),
                            "claim_handles": [gap_handle],
                            "evidence_handles": [],
                        }
                    ],
                    "notes": "",
                }
            )
        elif agent_name == "ChapterAssetBlockReviewReviserAgent":
            content = json.dumps(
                {
                    "paragraph_prose": (
                        "The reviewed material leaves open whether PINNs "
                        "outperform all classical solvers."
                    ),
                    "used_evidence_handles": [],
                }
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    compact_context = enhancer._build_compact_context(packet, ledger)
    review_data, diagnostics = enhancer._run_block_review_and_revision(
        blocks=blocks,
        plan=plan,
        ledger=ledger,
        compact_context=compact_context,
        reviewer_tier="c2_model",
        reviser_tier="c_model",
        qwen_caller=caller,
        usage_records=[],
    )

    assert review_data["blocking_count"] == 1
    assert sum(
        1
        for outcome in review_data["per_block_revision_outcomes"].values()
        if outcome.get("applied")
    ) == 1
    assert review_data["per_block_revision_outcomes"]["1"]["applied"] is True
    assert blocks[0]["prose"] == (
        "The reviewed material leaves open whether PINNs "
        "outperform all classical solvers."
    )
    assert calls == [
        "ChapterAssetBlockScientificReviewerAgent",
        "ChapterAssetBlockReviewReviserAgent",
    ]


def test_block_review_material_contradiction_revises_once_and_keeps_trust_planes(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    calls: list[str] = []

    def caller(agent_name: str, messages: list[dict], **_kwargs):
        calls.append(agent_name)
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 100,
            "output_tokens": 50,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": (
                        "PINNs learn high frequencies faster."
                    ),
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetBlockScientificReviewerAgent":
            content = json.dumps(
                {
                    "review_rows": [
                        {
                            "block_index": 1,
                            "sentence": "PINNs learn high frequencies faster.",
                            "flag_type": "material_contradiction",
                            "blocking": True,
                            "issue": (
                                "Direction reversal: evidence says higher "
                                "frequencies learn more slowly."
                            ),
                            "suggested_hedge": (
                                "PINNs learn high frequencies more slowly."
                            ),
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                        }
                    ],
                    "notes": "",
                }
            )
        elif agent_name == "ChapterAssetBlockReviewReviserAgent":
            content = json.dumps(
                {
                    "paragraph_prose": (
                        "PINNs learn high frequencies more slowly."
                    ),
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetExplanatoryCitationPlannerAgent":
            content = json.dumps(
                {
                    "explanatory_rows": [
                        {
                            "sentence_handle": "B01-S01",
                            "benefit_type": "definition",
                            "query": "PINN spectral bias",
                        }
                    ]
                }
            )
        elif agent_name == "ChapterAssetExplanatorySemanticRerankerAgent":
            payload = json.loads(messages[-1]["content"])
            content = json.dumps(
                {
                    "semantic_scores": [
                        {
                            "handle": candidate["handle"],
                            "helpfulness_score": 85,
                            "reason": "relevant",
                        }
                        for candidate in payload["candidate_table"]
                    ]
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    def local_search(query: str, max_results: int) -> list[dict]:
        return [
            {
                "paper_id": "review-paper",
                "title": "PINN Spectral Bias Review",
                "abstract": "PINN spectral bias and high frequency learning.",
                "relevance_score": 0.8,
            }
        ]

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-review-revise",
        live=True,
        qwen_caller=caller,
        local_search_callback=local_search,
    )

    assert report["status"] == "enhanced"
    assert calls == [
        "ChapterAssetArgumentPlannerAgent",
        "ChapterAssetExplanationBlockWriterAgent",
        "ChapterAssetLegacyGapAuditorAgent",
        "ChapterAssetBlockScientificReviewerAgent",
        "ChapterAssetBlockReviewReviserAgent",
        "ChapterAssetExplanatoryCitationPlannerAgent",
        "ChapterAssetExplanatorySemanticRerankerAgent",
    ]
    block_review = report["block_scientific_review"]
    assert block_review["available"] is True
    assert block_review["blocking_count"] == 1
    assert block_review["revision_applied_count"] == 1
    blocks = json.loads(
        (
            tmp_path
            / "out-review-revise"
            / "EXPLANATION_BLOCKS.json"
        ).read_text(encoding="utf-8")
    )["blocks"]
    assert "more slowly" in blocks[0]["prose"]
    assert "faster" not in blocks[0]["prose"]
    claim_map = json.loads(
        (
            tmp_path
            / "out-review-revise"
            / "CLAIM_TO_PARAGRAPH_MAP.json"
        ).read_text(encoding="utf-8")
    )
    assert claim_map["claim_to_paragraph"]["C1"] == [1]
    ledger_data = json.loads(
        (
            tmp_path
            / "out-review-revise"
            / "EXPLANATORY_CITATION_LEDGER.json"
        ).read_text(encoding="utf-8")
    )
    assert len(ledger_data["records"]) == 1
    enhanced = (
        tmp_path / "out-review-revise" / "ENHANCED_CHAPTER.md"
    ).read_text(encoding="utf-8")
    assert "[REF:review-paper]" in enhanced
    assert report["model_usage"]["call_count"] == 7
    assert report["model_usage"]["total_estimated_cost_cny"] > 0


def test_reviser_accepts_same_planned_claim_unused_evidence(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    e2 = ledger.evidence_by_claim["C1"][1].handle

    def caller(agent_name: str, *_args, **_kwargs):
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 80,
            "output_tokens": 40,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "Original block prose.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        elif agent_name == "ChapterAssetBlockScientificReviewerAgent":
            content = json.dumps(
                {
                    "review_rows": [
                        {
                            "block_index": 1,
                            "sentence": "Original block prose.",
                            "flag_type": "material_contradiction",
                            "blocking": True,
                            "issue": "unsupported guarantee",
                            "suggested_hedge": "hedge",
                            "claim_handles": [c1],
                            "evidence_handles": [e2],
                        }
                    ],
                    "notes": "",
                }
            )
        elif agent_name == "ChapterAssetBlockReviewReviserAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "Revised block prose.",
                    "used_evidence_handles": [e2],
                }
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-reviser-scope-accept",
        live=True,
        qwen_caller=caller,
    )

    assert report["status"] == "enhanced"
    review = json.loads(
        (
            tmp_path
            / "out-reviser-scope-accept"
            / "BLOCK_SCIENTIFIC_REVIEW.json"
        ).read_text(encoding="utf-8")
    )
    assert review["per_block_revision_outcomes"]["1"]["applied"] is True
    blocks = json.loads(
        (
            tmp_path
            / "out-reviser-scope-accept"
            / "EXPLANATION_BLOCKS.json"
        ).read_text(encoding="utf-8")
    )["blocks"]
    assert blocks[0]["prose"] == "Revised block prose."
    assert set(blocks[0]["evidence_handles"]) == {e1, e2}


def test_reviser_preserves_cross_claim_evidence_for_downstream_correction(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    e3 = ledger.evidence_by_claim["C2"][0].handle

    def caller(agent_name: str, *_args, **_kwargs):
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 80,
            "output_tokens": 40,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "Original block prose.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        elif agent_name == "ChapterAssetBlockScientificReviewerAgent":
            content = json.dumps(
                {
                    "review_rows": [
                        {
                            "block_index": 1,
                            "sentence": "Original block prose.",
                            "flag_type": "material_contradiction",
                            "blocking": True,
                            "issue": "unsupported guarantee",
                            "suggested_hedge": "hedge",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                        }
                    ],
                    "notes": "",
                }
            )
        elif agent_name == "ChapterAssetBlockReviewReviserAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "Invalid cross-claim revision.",
                    "used_evidence_handles": [e3],
                }
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-reviser-scope-reject",
        live=True,
        qwen_caller=caller,
    )

    assert report["status"] == "enhanced"
    review = json.loads(
        (
            tmp_path
            / "out-reviser-scope-reject"
            / "BLOCK_SCIENTIFIC_REVIEW.json"
        ).read_text(encoding="utf-8")
    )
    assert review["per_block_revision_outcomes"]["1"]["applied"] is True
    blocks = json.loads(
        (
            tmp_path
            / "out-reviser-scope-reject"
            / "EXPLANATION_BLOCKS.json"
        ).read_text(encoding="utf-8")
    )["blocks"]
    assert blocks[0]["prose"] == "Invalid cross-claim revision."
    assert blocks[0]["evidence_handles"] == [e1, e3]


def test_block_review_advisory_only_does_not_revise(tmp_path: Path) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    calls: list[str] = []

    def caller(agent_name: str, *_args, **_kwargs):
        calls.append(agent_name)
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 60,
            "output_tokens": 30,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "A PINN embeds physics in the loss.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetBlockScientificReviewerAgent":
            content = json.dumps(
                {
                    "review_rows": [
                        {
                            "block_index": 1,
                            "sentence": "A PINN embeds physics in the loss.",
                            "flag_type": "advisory",
                            "blocking": True,
                            "issue": "Could be phrased more carefully.",
                            "suggested_hedge": "",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                        }
                    ],
                    "notes": "",
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-review-advisory",
        live=True,
        qwen_caller=caller,
    )

    assert report["status"] == "enhanced"
    assert "ChapterAssetBlockReviewReviserAgent" not in calls
    assert report["block_scientific_review"]["advisory_count"] == 1
    assert report["block_scientific_review"]["blocking_count"] == 0
    blocks = json.loads(
        (
            tmp_path
            / "out-review-advisory"
            / "EXPLANATION_BLOCKS.json"
        ).read_text(encoding="utf-8")
    )["blocks"]
    assert blocks[0]["prose"] == "A PINN embeds physics in the loss."


def test_overclaim_and_unsupported_rows_never_trigger_revision(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    calls: list[str] = []

    def caller(agent_name: str, *_args, **_kwargs):
        calls.append(agent_name)
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 60,
            "output_tokens": 30,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "A PINN embeds physics in the loss.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetBlockScientificReviewerAgent":
            content = json.dumps(
                {
                    "review_rows": [
                        {
                            "block_index": 1,
                            "sentence": "A PINN embeds physics in the loss.",
                            "flag_type": "material_overclaim",
                            "blocking": True,
                            "issue": "unsupported guarantee",
                            "suggested_hedge": "downstream hedge one",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                        },
                        {
                            "block_index": 1,
                            "sentence": "A PINN embeds physics in the loss.",
                            "flag_type": "unsupported_new_mechanism",
                            "blocking": True,
                            "issue": "new mechanism not in evidence",
                            "suggested_hedge": "downstream hedge two",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                        },
                    ],
                    "notes": "",
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-review-nonblocking",
        live=True,
        qwen_caller=caller,
    )

    assert report["status"] == "enhanced"
    assert "ChapterAssetBlockReviewReviserAgent" not in calls
    assert report["block_scientific_review"]["blocking_count"] == 0
    assert report["block_scientific_review"]["advisory_count"] == 2
    review = json.loads(
        (
            tmp_path
            / "out-review-nonblocking"
            / "BLOCK_SCIENTIFIC_REVIEW.json"
        ).read_text(encoding="utf-8")
    )
    assert {row["flag_type"] for row in review["comments"]} == {
        "material_overclaim",
        "unsupported_new_mechanism",
    }
    assert {row["suggested_hedge"] for row in review["comments"]} == {
        "downstream hedge one",
        "downstream hedge two",
    }


def test_reviewer_sees_patch_and_revision_reaches_explanatory_planner(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    calls: list[dict] = []

    def caller(agent_name: str, messages: list[dict], **_kwargs):
        calls.append(
            {"agent_name": agent_name, "messages": messages}
        )
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 100,
            "output_tokens": 50,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "Original core prose.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {
                    "verdict": "gaps_found",
                    "gaps": [
                        {
                            "gap_id": "G01",
                            "old_draft_snippet": "old",
                            "scientific_content": "missing detail",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "affected_block_indices": [1],
                        }
                    ],
                    "notes": "",
                }
            )
        elif agent_name == "ChapterAssetLegacyGapPatchWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "Patched core prose.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetBlockScientificReviewerAgent":
            content = json.dumps(
                {
                    "review_rows": [
                        {
                            "block_index": 1,
                            "sentence": "Patched core prose.",
                            "flag_type": "material_contradiction",
                            "blocking": True,
                            "issue": "unsupported guarantee",
                            "suggested_hedge": "hedge",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                        }
                    ],
                    "notes": "",
                }
            )
        elif agent_name == "ChapterAssetBlockReviewReviserAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "Revised patched prose.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetExplanatoryCitationPlannerAgent":
            content = json.dumps(
                {
                    "explanatory_rows": [
                        {
                            "sentence_handle": "B01-S01",
                            "benefit_type": "definition",
                            "query": "PINN background",
                        }
                    ]
                }
            )
        elif agent_name == "ChapterAssetExplanatorySemanticRerankerAgent":
            payload = json.loads(messages[-1]["content"])
            content = json.dumps(
                {
                    "semantic_scores": [
                        {
                            "handle": candidate["handle"],
                            "helpfulness_score": 85,
                            "reason": "relevant",
                        }
                        for candidate in payload["candidate_table"]
                    ]
                }
            )
        elif agent_name == "ChapterAssetRepresentativeApplicationWriterAgent":
            payload = json.loads(messages[-1]["content"])
            content = json.dumps(
                {
                    "application_rows": [
                        {
                            "target_handle": row["target_handle"],
                            "prose": (
                                "One study applied the shared concept in "
                                "practice and reported a measured gain."
                            ),
                        }
                        for row in payload["targets"]
                    ]
                }
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    def local_search(query: str, max_results: int) -> list[dict]:
        return [
            {
                "paper_id": "after-patch-paper",
                "title": "PINN Background Paper",
                "abstract": "PINN background theory.",
                "relevance_score": 0.8,
            }
        ]

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-review-after-patch",
        live=True,
        qwen_caller=caller,
        local_search_callback=local_search,
    )

    assert [call["agent_name"] for call in calls] == [
        "ChapterAssetArgumentPlannerAgent",
        "ChapterAssetExplanationBlockWriterAgent",
        "ChapterAssetLegacyGapAuditorAgent",
        "ChapterAssetLegacyGapPatchWriterAgent",
        "ChapterAssetBlockScientificReviewerAgent",
        "ChapterAssetBlockReviewReviserAgent",
        "ChapterAssetExplanatoryCitationPlannerAgent",
        "ChapterAssetExplanatorySemanticRerankerAgent",
    ]
    reviewer_message = next(
        call["messages"][-1]["content"]
        for call in calls
        if call["agent_name"] == "ChapterAssetBlockScientificReviewerAgent"
    )
    assert "Patched core prose." in reviewer_message
    explanatory_message = next(
        call["messages"][-1]["content"]
        for call in calls
        if call["agent_name"] == "ChapterAssetExplanatoryCitationPlannerAgent"
    )
    assert "Revised patched prose." in explanatory_message
    enhanced = (
        tmp_path / "out-review-after-patch" / "ENHANCED_CHAPTER.md"
    ).read_text(encoding="utf-8")
    assert "Revised patched prose." in enhanced
    assert "[REF:after-patch-paper]" in enhanced
    assert report["block_scientific_review"]["revision_applied_count"] == 1


def test_block_review_failure_fails_open_and_keeps_blocks(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle

    def caller(agent_name: str, *_args, **_kwargs):
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 60,
            "output_tokens": 30,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "A PINN embeds physics in the loss.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetBlockScientificReviewerAgent":
            raise RuntimeError("reviewer transport down")
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-review-fail",
        live=True,
        qwen_caller=caller,
    )

    assert report["status"] == "enhanced"
    assert report["block_scientific_review"]["attempted"] is True
    assert report["block_scientific_review"]["available"] is False
    assert report["block_scientific_review"]["fallback_reason"]
    assert (tmp_path / "out-review-fail" / "BLOCK_SCIENTIFIC_REVIEW.json").exists()
    blocks = json.loads(
        (
            tmp_path
            / "out-review-fail"
            / "EXPLANATION_BLOCKS.json"
        ).read_text(encoding="utf-8")
    )["blocks"]
    assert blocks[0]["prose"] == "A PINN embeds physics in the loss."


def test_block_review_non_list_rows_fails_open_as_unavailable(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle

    def caller(agent_name: str, *_args, **_kwargs):
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 60,
            "output_tokens": 30,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "A PINN embeds physics in the loss.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetBlockScientificReviewerAgent":
            content = json.dumps({"review_rows": "not-a-list", "notes": ""})
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-review-nonlist",
        live=True,
        qwen_caller=caller,
    )

    assert report["status"] == "enhanced"
    assert report["block_scientific_review"]["available"] is False
    assert report["block_scientific_review"]["fallback_reason"] == (
        "malformed_output"
    )
    blocks = json.loads(
        (
            tmp_path
            / "out-review-nonlist"
            / "EXPLANATION_BLOCKS.json"
        ).read_text(encoding="utf-8")
    )["blocks"]
    assert blocks[0]["prose"] == "A PINN embeds physics in the loss."


def test_block_revision_unknown_handle_is_preserved_without_marker(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle

    def caller(agent_name: str, *_args, **_kwargs):
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 60,
            "output_tokens": 30,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "Original evidence-grounded prose.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetBlockScientificReviewerAgent":
            content = json.dumps(
                {
                    "review_rows": [
                        {
                            "block_index": 1,
                            "sentence": "Original evidence-grounded prose.",
                            "flag_type": "material_contradiction",
                            "blocking": True,
                            "issue": "unsupported guarantee",
                            "suggested_hedge": "hedge",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                        }
                    ],
                    "notes": "",
                }
            )
        elif agent_name == "ChapterAssetBlockReviewReviserAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "Invalid revision.",
                    "used_evidence_handles": ["E99_UNKNOWN"],
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-reviser-fail",
        live=True,
        qwen_caller=caller,
    )

    assert report["status"] == "enhanced"
    review_artifact = json.loads(
        (
            tmp_path
            / "out-reviser-fail"
            / "BLOCK_SCIENTIFIC_REVIEW.json"
        ).read_text(encoding="utf-8")
    )
    outcome = review_artifact["per_block_revision_outcomes"]
    assert outcome["1"]["applied"] is True
    blocks = json.loads(
        (
            tmp_path
            / "out-reviser-fail"
            / "EXPLANATION_BLOCKS.json"
        ).read_text(encoding="utf-8")
    )["blocks"]
    assert blocks[0]["prose"] == "Invalid revision."
    assert blocks[0]["evidence_handles"] == [e1, "E99_UNKNOWN"]
    enhanced = (
        tmp_path / "out-reviser-fail" / "ENHANCED_CHAPTER.md"
    ).read_text(encoding="utf-8")
    assert "Invalid revision." in enhanced
    assert "E99_UNKNOWN" not in enhanced


def test_block_review_unknown_block_handle_cannot_mutate_blocks(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    calls: list[str] = []

    def caller(agent_name: str, *_args, **_kwargs):
        calls.append(agent_name)
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 60,
            "output_tokens": 30,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "A PINN embeds physics in the loss.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetBlockScientificReviewerAgent":
            content = json.dumps(
                {
                    "review_rows": [
                        {
                            "block_index": 99,
                            "sentence": "unknown block",
                            "flag_type": "material_contradiction",
                            "blocking": True,
                            "issue": "bad",
                            "suggested_hedge": "hedge",
                            "claim_handles": ["C99_UNKNOWN"],
                            "evidence_handles": ["E99_UNKNOWN"],
                        }
                    ],
                    "notes": "",
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-review-unknown",
        live=True,
        qwen_caller=caller,
    )

    assert report["status"] == "enhanced"
    assert "ChapterAssetBlockReviewReviserAgent" not in calls
    review_artifact = json.loads(
        (
            tmp_path
            / "out-review-unknown"
            / "BLOCK_SCIENTIFIC_REVIEW.json"
        ).read_text(encoding="utf-8")
    )
    assert review_artifact["comments"] == []
    assert report["block_scientific_review"]["blocking_count"] == 0
    blocks = json.loads(
        (
            tmp_path
            / "out-review-unknown"
            / "EXPLANATION_BLOCKS.json"
        ).read_text(encoding="utf-8")
    )["blocks"]
    assert blocks[0]["prose"] == "A PINN embeds physics in the loss."


def test_semantic_reranker_removes_only_clear_topic_mismatches(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    rows = [
        {
            "sentence_handle": "B01-S01",
            "benefit_type": "mechanism_background",
            "query": "PINN uncertainty quantification",
        },
        {
            "sentence_handle": "B01-S01",
            "benefit_type": "mechanism_background",
            "query": "mid-infrared refractive index measurement",
        },
        {
            "sentence_handle": "B01-S01",
            "benefit_type": "mechanism_background",
            "query": "environmental life cycle assessment uncertainty",
        },
        {
            "sentence_handle": "B01-S01",
            "benefit_type": "definition",
            "query": "PINN theory background",
        },
    ]

    def caller(agent_name: str, messages: list[dict], **_kwargs):
        usage = {
            "model_name": "qwen3.7-flash",
            "input_tokens": 120,
            "output_tokens": 60,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "A PINN embeds physics in the loss.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetExplanatoryCitationPlannerAgent":
            content = json.dumps({"explanatory_rows": rows})
        elif agent_name == "ChapterAssetExplanatorySemanticRerankerAgent":
            payload = json.loads(messages[-1]["content"])
            semantic_scores = []
            for candidate in payload["candidate_table"]:
                title = candidate["title"]
                if "PINN UQ" in title:
                    score = 85
                elif "PINN Background" in title:
                    score = 55
                elif "Infrared" in title:
                    score = 0
                elif "LCA" in title:
                    score = 15
                else:
                    score = 50
                semantic_scores.append(
                    {
                        "handle": candidate["handle"],
                        "helpfulness_score": score,
                        "reason": "rubric-based test score",
                    }
                )
            content = json.dumps({"semantic_scores": semantic_scores})
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    def local_search(query: str, max_results: int) -> list[dict]:
        if "infrared" in query:
            return [
                {
                    "paper_id": "infrared-1",
                    "title": "Mid-Infrared Refractive Index Measurement",
                    "abstract": "Mid-infrared refractive index measurement setup.",
                }
            ]
        if "life cycle" in query:
            return [
                {
                    "paper_id": "lca-1",
                    "title": "Environmental LCA Uncertainty",
                    "abstract": "Environmental life cycle assessment uncertainty.",
                }
            ]
        if "uncertainty quantification" in query:
            return [
                {
                    "paper_id": "pinn-uq-1",
                    "title": "PINN UQ Survey",
                    "abstract": "Uncertainty quantification for PINN predictions.",
                }
            ]
        return [
            {
                "paper_id": "pinn-bg-1",
                "title": "PINN Background Theory",
                "abstract": "Background theory for PINN residual training.",
            }
        ]

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-semantic-rerank",
        live=True,
        qwen_caller=caller,
        local_search_callback=local_search,
    )

    ledger_data = json.loads(
        (
            tmp_path
            / "out-semantic-rerank"
            / "EXPLANATORY_CITATION_LEDGER.json"
        ).read_text(encoding="utf-8")
    )
    titles = {record["metadata"]["title"] for record in ledger_data["records"]}
    assert "PINN UQ Survey" in titles
    assert "PINN Background Theory" in titles
    assert "Mid-Infrared Refractive Index Measurement" not in titles
    assert "Environmental LCA Uncertainty" not in titles
    semantic = ledger_data["selection_audit"]["semantic_rerank"]
    assert semantic["available"] is True
    assert semantic["scored_count"] == 4
    assert semantic["rejected_clear_mismatch_count"] == 2
    enhanced = (
        tmp_path / "out-semantic-rerank" / "ENHANCED_CHAPTER.md"
    ).read_text(encoding="utf-8")
    assert "[REF:infrared-1]" not in enhanced
    assert "[REF:lca-1]" not in enhanced
    claim_map = json.loads(
        (
            tmp_path
            / "out-semantic-rerank"
            / "CLAIM_TO_PARAGRAPH_MAP.json"
        ).read_text(encoding="utf-8")
    )
    assert claim_map["claim_to_paragraph"]["C1"] == [1]
    assert report["coverage_metrics"]["actual_covered_claim_count"] == 1


def test_semantic_reranker_failure_fails_open_to_deterministic_selection(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    caller, _calls = _explanatory_fake_caller(
        ledger,
        explanatory_rows=[
            {
                "sentence_handle": "B01-S01",
                "benefit_type": "definition",
                "query": "PINN residual loss",
            }
        ],
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return [
            {
                "paper_id": "deterministic-paper",
                "title": "Deterministic PINN Paper",
                "abstract": "PINN residual loss background.",
                "relevance_score": 0.8,
            }
        ]

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-rerank-fail",
        live=True,
        qwen_caller=caller,
        local_search_callback=local_search,
    )

    assert report["status"] == "enhanced"
    ledger_data = json.loads(
        (
            tmp_path
            / "out-rerank-fail"
            / "EXPLANATORY_CITATION_LEDGER.json"
        ).read_text(encoding="utf-8")
    )
    assert len(ledger_data["records"]) == 1
    semantic = ledger_data["selection_audit"]["semantic_rerank"]
    assert semantic["attempted"] is True
    assert semantic["available"] is False
    assert semantic["fallback_reason"]
    assert any(
        "semantic_reranker_unavailable" in diagnostic
        for diagnostic in ledger_data["diagnostics"]
    )
    assert report["coverage_metrics"]["actual_covered_claim_count"] == 1


def test_partial_semantic_reranker_falls_back_to_deterministic_selection(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle

    def caller(agent_name: str, messages: list[dict], **_kwargs):
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 80,
            "output_tokens": 40,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            content = json.dumps(
                {
                    "paragraph_prose": "A PINN embeds physics in the loss.",
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetBlockScientificReviewerAgent":
            content = json.dumps({"review_rows": [], "notes": ""})
        elif agent_name == "ChapterAssetExplanatoryCitationPlannerAgent":
            content = json.dumps(
                {
                    "explanatory_rows": [
                        {
                            "sentence_handle": "B01-S01",
                            "benefit_type": "definition",
                            "query": "PINN background A",
                        },
                        {
                            "sentence_handle": "B01-S01",
                            "benefit_type": "mechanism_background",
                            "query": "PINN background B",
                        },
                    ]
                }
            )
        elif agent_name == "ChapterAssetExplanatorySemanticRerankerAgent":
            payload = json.loads(messages[-1]["content"])
            table = payload["candidate_table"]
            content = json.dumps(
                {
                    "semantic_scores": [
                        {
                            "handle": table[0]["handle"],
                            "helpfulness_score": 85,
                            "reason": "only one scored",
                        }
                    ]
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {"verdict": "no_actionable_gaps", "gaps": [], "notes": ""}
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    def local_search(query: str, max_results: int) -> list[dict]:
        if "A" in query:
            return [
                {
                    "paper_id": "paper-a",
                    "title": "PINN Background A",
                    "abstract": "PINN background A.",
                }
            ]
        return [
            {
                "paper_id": "paper-b",
                "title": "PINN Background B",
                "abstract": "PINN background B.",
            }
        ]

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-partial-rerank",
        live=True,
        qwen_caller=caller,
        local_search_callback=local_search,
    )

    assert report["status"] == "enhanced"
    ledger_data = json.loads(
        (
            tmp_path
            / "out-partial-rerank"
            / "EXPLANATORY_CITATION_LEDGER.json"
        ).read_text(encoding="utf-8")
    )
    assert len(ledger_data["records"]) == 2
    assert all("helpfulness_score" not in record for record in ledger_data["records"])
    semantic = ledger_data["selection_audit"]["semantic_rerank"]
    assert semantic["available"] is False
    assert semantic["fallback_reason"] == "incomplete_semantic_rerank"
    assert any(
        "incomplete" in diagnostic
        for diagnostic in ledger_data["diagnostics"]
    )


def test_invalid_repair_output_records_repaired_false(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()

    def caller(agent_name: str, *_args, **_kwargs):
        usage = {
            "model_name": "qwen3.5-plus",
            "input_tokens": 80,
            "output_tokens": 40,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = "invalid-json"
        elif agent_name == "ChapterAssetContractRepairAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": 1,
                            "title": "Block",
                            "block_type": "explanatory_body",
                            "goal": "Explain.",
                            "claim_handles": ["C99_UNKNOWN"],
                            "evidence_handles": [],
                            "omitted_handle_reasons": {},
                        }
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-invalid-repair",
        live=True,
        qwen_caller=caller,
    )

    assert report["status"] == "fail_open_original"
    diagnostics = report["recovery_diagnostics"]
    assert diagnostics[-1]["repaired"] is False
    assert "unknown claim handle" not in diagnostics[-1]["error"]
    assert "empty prose" in diagnostics[-1]["error"]
    assert any(row.get("repaired") for row in diagnostics[:-1])
    assert diagnostics[-1]["repaired"] is False


def test_task_specific_repair_token_budgets() -> None:
    packet_data = _fake_packet_data()
    packet = enhancer._rehydrate_packet(packet_data)
    ledger = enhancer._build_handle_ledger(packet)
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    compact_context = enhancer._build_compact_context(packet, ledger)
    calls: list[tuple[str, int]] = []
    usage_records: list[dict] = []
    recovery_diagnostics: list[dict] = []

    def fake_caller(agent_name: str, *_args, **kwargs):
        calls.append((agent_name, kwargs.get("max_tokens")))
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            return {"content": "invalid-json", "_llm_usage": {}}
        if agent_name == "ChapterAssetContractRepairAgent":
            return {
                "content": json.dumps(
                    {
                        "chapter_thesis": "Thesis.",
                        "reader_takeaway": "Takeaway.",
                        "argument_sequence": [],
                        "terminology_rows": [],
                        "explanation_block_rows": [
                            {
                                "block_index": 1,
                                "title": "Block",
                                "block_type": "explanatory_body",
                                "goal": "Explain.",
                                "claim_handles": [c1],
                                "evidence_handles": [e1],
                                "omitted_handle_reasons": {},
                            }
                        ],
                        "omitted_handle_reasons": {},
                    }
                ),
                "_llm_usage": {},
            }
        return {"content": "", "_llm_usage": {}}

    enhancer._run_planner(
        compact_context=compact_context,
        ledger=ledger,
        model_tier="c_model",
        repair_tier="c2_model",
        qwen_caller=fake_caller,
        usage_records=usage_records,
        recovery_diagnostics=recovery_diagnostics,
    )
    assert calls[0] == ("ChapterAssetArgumentPlannerAgent", 8000)
    assert calls[1] == ("ChapterAssetContractRepairAgent", 8000)

    block_calls: list[tuple[str, int]] = []

    def block_fake_caller(agent_name: str, *_args, **kwargs):
        block_calls.append((agent_name, kwargs.get("max_tokens")))
        return {
            "content": json.dumps(
                {
                    "paragraph_prose": "Revised block prose.",
                    "used_evidence_handles": [e1],
                }
            ),
            "_llm_usage": {},
        }

    enhancer._repair_block_output(
        original_payload={},
        allowed_handles={e1},
        ledger=ledger,
        requires_evidence=True,
        invalid_output="",
        validation_error="test",
        repair_tier="c2_model",
        qwen_caller=block_fake_caller,
        usage_records=usage_records,
        recovery_diagnostics=[],
    )
    assert block_calls[0] == ("ChapterAssetContractRepairAgent", 5000)
