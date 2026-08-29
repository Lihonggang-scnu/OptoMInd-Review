from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import optomind_research.publication_revision_loop as revision_loop_module

from optomind_research.full_review_state import FullReviewState, PIPELINE_STAGE_IDS
from optomind_research.publication_revision_loop import (
    ConvergenceController,
    ReviewerIssueCompiler,
    RevisionDecisionBoard,
    TargetedRevisionExecutor,
    run_publication_revision_loop,
    _match_issue_lineage,
    _salvage_complete_array_objects,
    _salvage_complete_tasks,
)
from optomind_research.gap_resolution_agent import GapResolutionAgent
from optomind_research.full_review_evidence import (
    merge_incremental_material_packets,
    refresh_argument_dag_evidence_state,
    resolve_evidence_gaps,
)


def _revision(text: str = "A bounded mechanism is supported [REF:p1:C1].") -> dict:
    return {
        "blueprint": {
            "sections": [{
                "section_id": "S01",
                "title": "Mechanism",
                "claims": [{
                    "claim_id": "S01-C01",
                    "statement": "A bounded mechanism is supported.",
                    "load_bearing": True,
                    "evidence_requirement": "factual",
                }],
            }]
        },
        "section_drafts": [{
            "section_id": "S01",
            "english_text": text,
            "citation_map": {"0": ["chunk-1"]},
            "overclaim_flags": [],
            "contradiction_notes": [],
            "figure_placements": [],
            "status": "audited",
            "uncited_load_bearing": [],
            "revision_history": [],
        }],
        "material_packets": [{
            "section_id": "S01",
            "section_contract": {"title": "Mechanism"},
            "claims": [{
                "claim_id": "S01-C01",
                "statement": "A bounded mechanism is supported.",
                "load_bearing": True,
            }],
            "evidence_packets": [{
                "claim_id": "C1",
                "paper_id": "p1",
                "chunk_id": "chunk-1",
                "exact_spans": ["A bounded mechanism is supported."],
            }],
            "visual_evidence": [],
            "visual_gap_plan": [],
        }],
    }


def _citation_audit(*, invalid: int = 0, uncited: int = 0) -> dict:
    return {
        "invalid_citation_count": invalid,
        "uncited_load_bearing_claim_count": uncited,
        "citation_audits": [{
            "section_id": "S01",
            "invalid_cited_chunk_ids": ["bad"] if invalid else [],
            "uncited_load_bearing_claim_ids": ["S01-C01"] if uncited else [],
            "uncited_after_entailment_rejection": [],
            "section_quality_judgment": {"unsupported_fact_detected": False},
        }],
    }


def _global(issues: list[dict] | None = None, audit: dict | None = None) -> dict:
    return {
        "judgment": {
            "issues": issues or [],
            "formal_readiness": "ready_after_minor_revision",
            "overall_score": 4.2,
        },
        "post_revision_citation_audit": audit or _citation_audit(),
    }


def _peer(issues: list[dict] | None = None) -> dict:
    return {
        "peer_reviews": [{
            "reviewer_role": "domain_reviewer",
            "recommendation": "minor_revision",
            "issues": issues or [],
        }]
    }


def test_issue_compiler_merges_duplicate_evidence_reviews():
    global_issue = {
        "issue_id": "G1",
        "severity": "high",
        "section_ids": ["S01"],
        "issue_type": "citation",
        "description": "A load-bearing mechanism claim lacks verified citation support.",
        "recommended_action": "Retrieve component evidence or narrow the claim.",
    }
    peer_issue = {
        **global_issue,
        "issue_id": "P1",
        "issue_type": "evidence",
        "description": "The load-bearing mechanism claim has no verified citation support.",
    }
    report = ReviewerIssueCompiler().compile(
        revision_bundle=_revision(),
        global_bundle=_global([global_issue]),
        peer_bundle=_peer([peer_issue]),
    )

    evidence = [row for row in report["issues"] if row["root_cause"] == "missing_evidence"]
    assert len(evidence) == 1
    assert {row["source"] for row in evidence[0]["source_refs"]} == {
        "global_review", "peer_review"
    }


def test_deterministic_citation_failure_cannot_be_hidden_by_reviewer():
    report = ReviewerIssueCompiler().compile(
        revision_bundle=_revision(),
        global_bundle=_global([], _citation_audit(invalid=1)),
        peer_bundle=_peer([]),
    )
    assert any(
        row["severity"] == "critical" and row["root_cause"] == "missing_evidence"
        for row in report["issues"]
    )


def test_systematic_identity_requires_documented_method_protocol():
    revision = _revision("This systematic review establishes a bounded mechanism [REF:p1:C1].")
    report = ReviewerIssueCompiler().compile(
        revision_bundle=revision,
        global_bundle=_global(),
        peer_bundle=_peer(),
        charter={"scope_statement": "A critical review of optical mechanisms."},
    )
    method = [row for row in report["issues"] if row["root_cause"] == "methodology_identity"]
    assert len(method) == 1
    assert method[0]["repair_route"] == "charter_method_patch"


def test_decision_board_auto_repairs_evidence_but_escalates_architecture():
    issues = {
        "issues": [
            {
                "issue_id": "RI-E",
                "severity": "high",
                "root_cause": "missing_evidence",
                "repair_route": "evidence_retrieval",
                "section_ids": ["S01"],
                "claim_ids": ["S01-C01"],
                "description": "Missing support.",
                "requested_change": "Retrieve component support.",
            },
            {
                "issue_id": "RI-A",
                "severity": "high",
                "root_cause": "architecture",
                "repair_route": "blueprint_contract_patch",
                "section_ids": ["S01"],
                "claim_ids": [],
                "description": "The taxonomy conflicts with the central thesis.",
                "requested_change": "Rebuild the taxonomy.",
            },
        ]
    }
    plan = RevisionDecisionBoard(real_llm=False).plan(issues, charter={}, max_tasks=8)
    by_issue = {issue: task for task in plan["tasks"] for issue in task["issue_ids"]}
    assert by_issue["RI-E"]["auto_apply"] is True
    assert by_issue["RI-A"]["auto_apply"] is False
    assert RevisionDecisionBoard._auto_policy(
        "evidence_retrieval", "missing_evidence", "high",
        {"auto_apply_recommended": False},
    ) is True


def test_local_architecture_and_method_identity_can_be_repaired_safely():
    issues = {"issues": [
        {
            "issue_id": "RI-LOCAL-ARCH",
            "severity": "medium",
            "root_cause": "architecture",
            "repair_route": "blueprint_contract_patch",
            "section_ids": ["S01"],
            "description": "One section mixes mechanism and implementation at the same taxonomy level.",
        },
        {
            "issue_id": "RI-METHOD",
            "severity": "high",
            "root_cause": "methodology_identity",
            "repair_route": "charter_method_patch",
            "section_ids": ["S01"],
            "description": "Systematic identity is not documented.",
        },
    ]}
    plan = RevisionDecisionBoard(real_llm=False).plan(issues, charter={}, max_tasks=8)
    by_issue = {issue: task for task in plan["tasks"] for issue in task["issue_ids"]}
    assert by_issue["RI-LOCAL-ARCH"]["auto_apply"] is True
    assert by_issue["RI-METHOD"]["auto_apply"] is True


def test_architecture_patch_is_field_and_section_allowlisted():
    blueprint = {"sections": [{
        "section_id": "S01", "title": "Old", "argument_role": "Old role", "claims": [{"claim_id": "C1"}]
    }]}
    contracts = [{"section_id": "S01", "central_thesis": "Old thesis"}]
    patch = {
        "section_patches": [
            {
                "section_id": "S01",
                "blueprint_updates": {
                    "title": "Mechanism-first taxonomy",
                    "claims": [],
                    "central_question": "Forbidden scope change",
                },
                "contract_updates": {
                    "classification_framework": "Mechanism, then implementation, then constraints.",
                    "word_budget": 999999,
                },
                "revision_instruction": "Use a two-level hierarchy.",
            },
            {"section_id": "S99", "blueprint_updates": {"title": "Injected"}},
        ]
    }
    applied, derived = TargetedRevisionExecutor._apply_architecture_patch(
        blueprint,
        contracts,
        patch,
        [{"issue_ids": ["RI-A"], "section_ids": ["S01"]}],
    )
    assert applied == ["S01"]
    assert blueprint["sections"][0]["title"] == "Mechanism-first taxonomy"
    assert blueprint["sections"][0]["claims"] == [{"claim_id": "C1"}]
    assert "central_question" not in blueprint["sections"][0]
    assert contracts[0]["classification_framework"].startswith("Mechanism")
    assert "word_budget" not in contracts[0]
    assert derived[0]["repair_route"] == "section_local_rewrite"


def test_review_process_failure_routes_to_automatic_reviewer_retry():
    report = ReviewerIssueCompiler().compile(
        revision_bundle=_revision(),
        global_bundle=_global([{
            "issue_id": "G-ERROR",
            "severity": "high",
            "section_ids": [],
            "issue_type": "argument",
            "description": "The global judge returned invalid output.",
        }]),
        peer_bundle=_peer(),
    )
    issue = next(row for row in report["issues"] if row["issue_type"] == "review_process_error")
    plan = RevisionDecisionBoard(real_llm=False).plan(report, charter={}, max_tasks=8)
    task = next(row for row in plan["tasks"] if issue["issue_id"] in row["issue_ids"])
    assert task["repair_route"] == "reviewer_retry"
    assert task["auto_apply"] is True


def test_truncated_planner_json_salvages_only_complete_tasks():
    text = (
        '{"tasks": ['
        '{"issue_ids":["RI-1"],"repair_route":"evidence_retrieval"},'
        '{"issue_ids":["RI-2"],"repair_route":"section_local_rewrite"},'
        '{"issue_ids":["RI-3"],"repair_route":"claim_narrowing"'
    )
    tasks = _salvage_complete_tasks(text)
    assert [row["issue_ids"][0] for row in tasks] == ["RI-1", "RI-2"]


def test_revision_planner_uses_bounded_non_thinking_json_call(monkeypatch):
    calls = []

    def fake_call(*args, **kwargs):
        calls.append(kwargs)
        return {
            "content": json.dumps({
                "tasks": [{
                    "issue_ids": ["RI-1"],
                    "repair_route": "claim_narrowing",
                }]
            }),
            "_llm_usage": {"model": "test"},
        }

    monkeypatch.setattr(revision_loop_module, "call_qwen_chat", fake_call)
    board = RevisionDecisionBoard(real_llm=True)
    rows = board._llm_plan(
        [{"issue_id": "RI-1", "repair_route": "claim_narrowing"}],
        {},
        8,
    )

    assert len(rows) == 1
    assert calls[0]["stream"] is False
    assert calls[0]["enable_thinking"] is False
    assert calls[0]["max_tokens"] == 2200
    assert calls[0]["max_retries"] == 0
    assert calls[0]["max_transport_key_candidates"] == 1
    assert calls[0]["allow_model_fallback"] is False


def test_truncated_revision_json_salvages_complete_paragraph_edits():
    text = (
        '{"paragraph_edits": ['
        '{"paragraph_index":1,"replacement_text":"complete replacement"},'
        '{"paragraph_index":2,"replacement_text":"truncated'
    )
    edits = _salvage_complete_array_objects(text, "paragraph_edits")
    assert edits == [{"paragraph_index": 1, "replacement_text": "complete replacement"}]


def test_issue_lineage_matches_rephrased_same_claim_problem():
    before = [{
        "issue_id": "OLD",
        "root_cause": "missing_evidence",
        "issue_type": "evidence_gap",
        "section_ids": ["S06"],
        "claim_ids": ["S06-C04"],
        "description": "The condensation coupling claim lacks a direct citation.",
    }]
    after = [{
        "issue_id": "NEW",
        "root_cause": "missing_evidence",
        "issue_type": "evidence_gap",
        "section_ids": ["S06"],
        "claim_ids": ["S06-C04"],
        "description": "Direct evidence is still missing for latent heat coupling.",
    }]
    lineage, resolved, new = _match_issue_lineage(before, after)
    assert lineage[0]["before_issue_id"] == "OLD"
    assert lineage[0]["after_issue_id"] == "NEW"
    assert resolved == set()
    assert new == set()


def test_targeted_revision_retries_one_transient_failure(monkeypatch, tmp_path):
    source = " ".join(["baseline"] * 90) + " [REF:p1:C1]."
    replacement = " ".join(["revised"] * 90) + " [REF:p1:C1]."
    calls = []

    def flaky_call(*args, **kwargs):
        calls.append(args[0])
        if len(calls) == 1:
            raise RuntimeError("transient transport failure")
        return {
            "content": json.dumps({
                "paragraph_edits": [{
                    "paragraph_index": 0,
                    "replacement_text": replacement,
                    "issue_ids": ["RI-1"],
                }],
                "resolved_issue_ids": ["RI-1"],
                "unresolved_issue_ids": [],
                "changes": ["Revised one bounded paragraph."],
                "claim_state_updates": [],
            }),
            "_llm_usage": {"model": "test"},
        }

    monkeypatch.setattr(revision_loop_module, "call_qwen_chat", flaky_call)
    executor = TargetedRevisionExecutor(
        real_llm=True,
        kb_path=None,
        enable_external_oa=False,
        external_output_dir=tmp_path,
        max_external_rounds=0,
        max_external_claims=1,
        generate_conceptual_visuals=False,
        max_generated_visuals=0,
    )
    revised, audit = executor._revise_one(
        draft=SimpleNamespace(
            section_id="S01", english_text=source, figure_placements=[]
        ),
        packet=SimpleNamespace(
            claims=[{"claim_id": "S01-C01", "load_bearing": True}],
            evidence_packets=[SimpleNamespace(
                claim_id="C1",
                paper_id="p1",
                chunk_id="chunk-1",
                exact_spans=["bounded support"],
                scope_fit="direct",
                retrieval_role="support",
            )],
        ),
        contract={"section_id": "S01"},
        tasks=[{"issue_ids": ["RI-1"], "claim_ids": ["S01-C01"]}],
        neighbour_context={},
    )
    assert revised == replacement
    assert audit["accepted"] is True
    assert [row["parse_mode"] for row in audit["llm_attempts"]] == [
        "exception",
        "complete_json",
    ]
    assert calls == ["TargetedReviewRevision:S01", "TargetedReviewRevisionRetry:S01"]


def test_deterministic_rollback_detects_new_citation_regression():
    executor = TargetedRevisionExecutor(
        real_llm=False,
        kb_path=None,
        enable_external_oa=False,
        external_output_dir=Path("unused"),
        max_external_rounds=1,
        max_external_claims=1,
        generate_conceptual_visuals=False,
        max_generated_visuals=0,
    )
    rolled = executor._deterministic_rollback(
        original_bundle=_revision(),
        working_bundle=_revision("A changed section with a citation regression."),
        baseline_audit=_citation_audit(),
        post_audit=_citation_audit(invalid=1),
        candidate_section_ids=["S01"],
    )
    assert rolled == ["S01"]


def test_evidence_task_rewrites_only_explicit_claim_owner_sections():
    blueprint = {
        "sections": [
            {"section_id": "S01", "claims": [{"claim_id": "S01-C01"}]},
            {"section_id": "S02", "claims": [{"claim_id": "S02-C01"}]},
            {"section_id": "S03", "claims": [{"claim_id": "S03-C01"}]},
        ]
    }
    task = {
        "repair_route": "evidence_retrieval",
        "section_ids": ["S01", "S02", "S03"],
        "claim_ids": ["S02-C01"],
    }
    broad_task = {
        "repair_route": "evidence_retrieval",
        "section_ids": ["S01", "S02", "S03"],
        "claim_ids": [],
    }

    assert TargetedRevisionExecutor._rewrite_section_ids_for_task(task, blueprint) == ["S02"]
    assert TargetedRevisionExecutor._rewrite_section_ids_for_task(broad_task, blueprint) == []


def test_convergence_allows_text_candidate_with_visuals_pending():
    issue_report = {
        "issues": [{
            "issue_id": "V1",
            "severity": "high",
            "root_cause": "visual_empirical",
        }]
    }
    decision = ConvergenceController().decide(
        round_number=1,
        max_rounds=3,
        issue_report=issue_report,
        plan={"tasks": [{"auto_apply": False}]},
        delta={"hard_regression": False, "quality_delta": 1},
        citation_bundle=_citation_audit(),
        global_bundle=_global(),
        peer_bundle=_peer(),
        prior_rounds=[],
    )
    assert decision["action"] == "complete"
    assert decision["reason"] == "text_publication_candidate_visuals_pending"


def test_mock_loop_is_auditable_and_does_not_rewrite(tmp_path):
    revision = _revision()
    report = run_publication_revision_loop(
        revision,
        _global(),
        _peer(),
        supervisor_bundle={},
        charter={},
        contracts=[],
        kb_path=None,
        output_dir=tmp_path,
        real_llm=False,
        max_rounds=3,
    )
    assert report["final_revision_bundle"] == revision
    assert report["final_decision"]["reason"] == "mock_mode_revision_not_executed"
    assert (tmp_path / "round_01" / "reviewer_issues.json").exists()
    assert (tmp_path / "revision_loop_report.json").exists()


def test_identical_issue_set_reuses_saved_revision_plan(tmp_path, monkeypatch):
    kwargs = dict(
        supervisor_bundle={}, charter={}, contracts=[], kb_path=None,
        output_dir=tmp_path, real_llm=False, max_rounds=1,
    )
    run_publication_revision_loop(_revision(), _global(), _peer(), **kwargs)

    def must_not_replan(*args, **kwargs):
        raise AssertionError("an identical persisted issue set should reuse its plan")

    monkeypatch.setattr(RevisionDecisionBoard, "plan", must_not_replan)
    report = run_publication_revision_loop(_revision(), _global(), _peer(), **kwargs)
    assert report["rounds"][0]["plan"]["planner_resume"]["reused"] is True


def test_completed_round_resume_restores_immutable_outputs_without_replanning(
    tmp_path, monkeypatch
):
    round_dir = tmp_path / "round_01"
    round_dir.mkdir(parents=True)
    accepted = _revision("Accepted checkpoint text [REF:p1:C1].")
    citation = _citation_audit()
    global_review = _global()
    peer_review = _peer()
    summary = {
        "round_number": 1,
        "after_issue_report": {"issues": []},
        "decision": {"action": "complete", "reason": "quality_threshold_met"},
    }
    for name, payload in {
        "candidate_revision.json": accepted,
        "candidate_citation_audit.json": citation,
        "post_revision_global_review.json": global_review,
        "post_revision_peer_reviews.json": peer_review,
        "round_summary.json": summary,
    }.items():
        (round_dir / name).write_text(json.dumps(payload), encoding="utf-8")

    def must_not_replan(*args, **kwargs):
        raise AssertionError("a completed immutable round must not be recomputed")

    monkeypatch.setattr(RevisionDecisionBoard, "plan", must_not_replan)
    report = run_publication_revision_loop(
        _revision(), _global(), _peer(),
        supervisor_bundle={}, charter={}, contracts=[], kb_path=None,
        output_dir=tmp_path, real_llm=False, max_rounds=1,
    )

    assert report["final_revision_bundle"] == accepted
    assert report["final_decision"]["reason"] == "quality_threshold_met"


def test_resume_never_promotes_a_completed_rejected_candidate(tmp_path, monkeypatch):
    accepted = _revision("Accepted round-one text [REF:p1:C1].")
    rejected = _revision("Regressed round-two text with broken support.")
    for round_number, candidate, promoted, action in [
        (1, accepted, True, "continue"),
        (2, rejected, False, "needs_human"),
    ]:
        round_dir = tmp_path / f"round_{round_number:02d}"
        round_dir.mkdir(parents=True)
        summary = {
            "round_number": round_number,
            "after_issue_report": {"issues": []},
            "decision": {"action": action, "reason": "test"},
            "candidate_promoted": promoted,
        }
        for name, payload in {
            "candidate_revision.json": candidate,
            "candidate_citation_audit.json": _citation_audit(invalid=1 if not promoted else 0),
            "post_revision_global_review.json": _global(),
            "post_revision_peer_reviews.json": _peer(),
            "round_summary.json": summary,
        }.items():
            (round_dir / name).write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(
        RevisionDecisionBoard,
        "plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("completed rounds must not be replanned")
        ),
    )
    report = run_publication_revision_loop(
        _revision(), _global(), _peer(),
        supervisor_bundle={}, charter={}, contracts=[], kb_path=None,
        output_dir=tmp_path, real_llm=False, max_rounds=2,
    )
    assert report["final_revision_bundle"] == accepted
    assert report["accepted_version_round"] == 1
    assert report["rejected_candidate_rounds"] == [2]
    assert report["final_candidate_promoted"] is False


def test_executor_completed_checkpoint_is_resumable(tmp_path):
    executor = TargetedRevisionExecutor(
        real_llm=False,
        kb_path=None,
        enable_external_oa=False,
        external_output_dir=tmp_path,
        max_external_rounds=0,
        max_external_claims=1,
        generate_conceptual_visuals=False,
        max_generated_visuals=0,
        checkpoint_dir=tmp_path,
        resume=True,
    )
    plan = {"tasks": []}
    first = executor.execute(
        _revision(), plan, charter={}, contracts=[],
        baseline_citation_bundle=_citation_audit(),
    )
    checkpoint = json.loads((tmp_path / "execution_checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["status"] == "completed"
    second = executor.execute(
        _revision("This input must not replace the accepted checkpoint."),
        plan,
        charter={},
        contracts=[],
        baseline_citation_bundle=_citation_audit(),
    )
    assert second["revision_bundle"] == first["revision_bundle"]
    assert json.loads((tmp_path / "execution_progress.json").read_text(encoding="utf-8"))[
        "status"
    ] == "resumed_completed_checkpoint"


def test_targeted_gap_resolution_does_not_touch_unrequested_claims():
    blueprint = {"sections": [{
        "section_id": "S01",
        "candidate_text_chunk_ids": ["doi:10.1/a:chunk-1", "doi:10.1/b:chunk-2"],
        "claims": [
            {
                "claim_id": "S01-C01",
                "saturation_score": 0.5,
                "supporting_text_chunk_ids": ["doi:10.1/a:chunk-1"],
            },
            {
                "claim_id": "S01-C02",
                "saturation_score": 0.5,
                "supporting_text_chunk_ids": ["doi:10.1/a:chunk-1"],
            },
        ],
    }]}
    updated, results = GapResolutionAgent(real_llm=False).resolve_blueprint(
        blueprint, target_claim_ids=["S01-C01"]
    )
    claims = {row["claim_id"]: row for row in updated["sections"][0]["claims"]}
    assert [row.claim_id for row in results] == ["S01-C01"]
    assert claims["S01-C01"]["supporting_text_chunk_ids"] != ["doi:10.1/a:chunk-1"]
    assert claims["S01-C02"]["supporting_text_chunk_ids"] == ["doi:10.1/a:chunk-1"]


def test_targeted_evidence_refresh_preserves_semantic_edges():
    blueprint = {
        "sections": [
            {"section_id": "S01", "claims": [{
                "claim_id": "C1", "statement": "Mechanism.", "evidence_type": "mechanism",
                "evidence_requirement": "factual", "evidence_binding_status": "direct",
                "supporting_text_chunk_ids": ["p1:c1"], "missing_evidence_components": [],
            }]},
            {"section_id": "S02", "claims": [{
                "claim_id": "C2", "statement": "Application.", "evidence_type": "application",
                "evidence_requirement": "factual", "evidence_binding_status": "partial",
                "supporting_text_chunk_ids": ["p2:c1"], "missing_evidence_components": ["field test"],
            }]},
        ],
        "argument_dag": {"edges": [{
            "edge_id": "C1->C2", "source_claim_id": "C1", "target_claim_id": "C2",
            "source_section_id": "S01", "target_section_id": "S02",
            "relation_type": "applies_to", "edge_readiness": "grounded",
        }], "pruning_stats": {"original_semantic_build": True}},
    }
    updated = refresh_argument_dag_evidence_state(blueprint, target_claim_ids=["C2"])
    assert updated["argument_dag"]["edges"][0]["relation_type"] == "applies_to"
    assert updated["argument_dag"]["edges"][0]["edge_readiness"] == "provisional"
    assert updated["argument_dag"]["pruning_stats"]["original_semantic_build"] is True
    assert updated["argument_dag"]["pruning_stats"]["evidence_state_refresh"]["target_claim_ids"] == ["C2"]


def test_revision_gap_resolution_explicitly_skips_semantic_rebuild(tmp_path, monkeypatch):
    import optomind_research.full_review_evidence as module

    blueprint = {
        "sections": [{
            "section_id": "S01",
            "title": "Mechanism",
            "candidate_text_chunk_ids": ["doi:10.1/a:chunk-1", "doi:10.1/b:chunk-2"],
            "candidate_text_chunks": [],
            "claims": [{
                "claim_id": "C1", "statement": "Mechanism.", "evidence_type": "mechanism",
                "evidence_requirement": "factual", "evidence_binding_status": "partial",
                "supporting_text_chunk_ids": ["doi:10.1/a:chunk-1"], "load_bearing": True,
            }],
        }],
        "argument_dag": {"edges": [], "pruning_stats": {}},
    }

    def forbidden_rebuild(*args, **kwargs):
        raise AssertionError("evidence-only revision must not rebuild semantic edges")

    monkeypatch.setattr(module, "rebuild_argument_dag", forbidden_rebuild)
    result = resolve_evidence_gaps(
        {"blueprint": blueprint}, kb_path=None, real_llm=False,
        target_claim_ids=["C1"], dag_update_mode="refresh_existing_graph",
    )
    assert result["argument_dag_update_mode"] == "refresh_existing_graph"


def test_incremental_packet_merge_never_drops_valid_old_citation():
    old = [{
        "section_id": "S01",
        "claims": [{"claim_id": "C1", "statement": "Stable bounded claim."}],
        "evidence_packets": [{
            "claim_id": "C1", "chunk_id": "old:chunk", "paper_id": "p-old",
            "exact_spans": ["Stable bounded claim."],
        }],
        "visual_evidence": [],
    }]
    fresh = [{
        "section_id": "S01",
        "claims": [{"claim_id": "C1", "statement": "Stable bounded claim."}],
        "evidence_packets": [{
            "claim_id": "C1", "chunk_id": "new:chunk", "paper_id": "p-new",
            "exact_spans": ["New component support."],
        }],
        "visual_evidence": [],
    }]
    merged, audit = merge_incremental_material_packets(old, fresh)
    assert {row["chunk_id"] for row in merged[0]["evidence_packets"]} == {
        "old:chunk", "new:chunk"
    }
    assert audit["previous_packets_preserved"] == 1


def test_incremental_packet_merge_rejects_old_packet_after_claim_change():
    old = [{
        "section_id": "S01", "claims": [{"claim_id": "C1", "statement": "Old claim."}],
        "evidence_packets": [{"claim_id": "C1", "chunk_id": "old:chunk", "paper_id": "p"}],
    }]
    fresh = [{
        "section_id": "S01", "claims": [{"claim_id": "C1", "statement": "Changed claim."}],
        "evidence_packets": [],
    }]
    merged, _ = merge_incremental_material_packets(old, fresh)
    assert merged[0]["evidence_packets"] == []


def test_old_state_stage_order_migrates_revision_loop(tmp_path):
    state = FullReviewState.new(user_query="An optical review question.")
    data = state.to_dict()
    data["stage_order"] = [sid for sid in data["stage_order"] if sid != "S20_revision_loop"]
    data["stages"].pop("S20_revision_loop", None)
    path = tmp_path / "state.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = FullReviewState.load(path)
    assert "S20_revision_loop" in loaded.stage_order
    assert loaded.stage_order.index("S20_revision_loop") < loaded.stage_order.index(
        "S20_final_translation"
    )
    assert loaded.stages["S20_revision_loop"].status == "pending"


def test_revision_loop_contains_no_topic_specific_rules():
    source = (Path(__file__).parents[1] / "optomind_research" / "publication_revision_loop.py").read_text(
        encoding="utf-8"
    ).lower()
    for forbidden in ("radiative cooling", "arabidopsis", "greenhouse film", "metasurface-only"):
        assert forbidden not in source
    assert "S20_revision_loop" in PIPELINE_STAGE_IDS
