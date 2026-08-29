"""Focused S15 M4 integration-contract tests.

These tests exercise the real S15 production path (edit_cross_section) and
the orchestrator stage wiring with injected offline role providers. No
network or API calls are made; the deterministic provider supplies the
non-commander roles and the injected commander responder supplies the
structured patch sets under test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from optomind_research.full_review_orchestrator import FullReviewOrchestrator
from optomind_research.full_review_production import edit_cross_section
from optomind_research.full_review_state import FullReviewState
from optomind_research.runtime.global_manuscript_commander import (
    DeterministicRoleProvider,
)


def _default_claim(section_id: str) -> dict[str, Any]:
    return {
        "claim_id": f"{section_id}-CL01",
        "role": "load_bearing",
        "claim_state": "ready_for_write",
        "evidence_binding_status": "direct",
        "writing_permission": "factual_support",
        "parent_claim_id": f"{section_id}-CL00",
        "evidence_strength": "strong",
        "statement": f"{section_id} claim statement",
        "load_bearing": False,
        "evidence_requirement": "factual",
    }


def _packet_row(section_id: str, title: str) -> dict[str, Any]:
    return {
        "section_id": section_id,
        "section_contract": {
            "section_id": section_id,
            "title": title,
            "section_role": "body",
            "paragraph_functions": [
                {
                    "paragraph_index": 1,
                    "title": f"Opening paragraph of {section_id}",
                    "purpose": f"Purpose of paragraph 1 in {section_id}",
                    "claim_ids": [f"{section_id}-CL01"],
                    "transition_logic": f"Transition for {section_id}",
                },
                {
                    "paragraph_index": 2,
                    "title": f"Second paragraph of {section_id}",
                    "purpose": f"Purpose of paragraph 2 in {section_id}",
                    "claim_ids": [],
                    "transition_logic": f"Transition for {section_id}",
                },
            ],
            "word_budget": 600,
            "expected_visual_arguments": [],
            "must_cover": [f"must-cover-{section_id}"],
            "must_not_cover": [f"prohibited-{section_id}"],
            "unique_contribution": f"Unique contribution for {section_id}",
        },
        "claims": [_default_claim(section_id)],
        "evidence_packets": [
            {
                "claim_id": f"{section_id}-CL01",
                "chunk_id": f"chunk-{section_id}-1",
                "paper_id": f"paper_{section_id}",
                "source_title": f"Paper title {section_id}",
                "exact_spans": [f"Quote for {section_id}"],
                "limitations": [],
                "support_relation": "direct",
                "evidence_level": "fulltext",
                "source_kind": "s2_body",
                "scope_fit": "in_domain",
                "retrieval_role": "evidence_candidate",
            }
        ],
        "contradictions": [],
        "open_questions": [],
        "transition_contract": {},
        "uncited_load_bearing_claim_ids": [],
        "visual_evidence": [],
        "visual_gap_plan": [],
        "manuscript_context": {
            "source_section_title": title,
            "current_section_boundary_contract": {
                "section_id": section_id,
                "title": title,
                "handoff_from_previous": f"handoff-from-{section_id}",
                "handoff_to_next": f"handoff-to-{section_id}",
                "must_not_cover": [f"boundary-prohibited-{section_id}"],
                "unique_contribution": (
                    f"Boundary unique contribution for {section_id}"
                ),
            },
            "sibling_section_responsibilities": [
                {"section_id": "S02", "responsibility": "sibling boundary"}
            ],
            "write_gate": {"allowed_to_write": True},
            "full_section_workplan": [
                {
                    "section_id": section_id,
                    "must_not_cover": [f"workplan-prohibited-{section_id}"],
                    "unique_contribution": (
                        f"Workplan unique contribution for {section_id}"
                    ),
                }
            ],
        },
        "literature_coverage": {
            "sources": [],
            "paper_ids": [],
            "evidence_chunk_ids": [],
        },
    }


def _draft_bundle(
    *,
    s01_text: str | None = None,
    s02_text: str | None = None,
) -> dict[str, Any]:
    drafts: list[dict[str, Any]] = []
    packets: list[dict[str, Any]] = []
    blueprint_sections: list[dict[str, Any]] = []
    for section_id, title, default_text in (
        (
            "S01",
            "First Chapter",
            "# First Chapter\n\n"
            "Opening paragraph of S01 with [REF:paper_S01].\n\n"
            "Second paragraph of S01.\n",
        ),
        (
            "S02",
            "Second Chapter",
            "# Second Chapter\n\n"
            "Opening paragraph of S02 with [REF:paper_S02].\n\n"
            "Second paragraph of S02.\n",
        ),
    ):
        text = (
            s01_text
            if section_id == "S01" and s01_text is not None
            else s02_text
            if section_id == "S02" and s02_text is not None
            else default_text
        )
        drafts.append(
            {
                "section_id": section_id,
                "english_text": text,
                "chinese_text": "",
                "citation_map": {},
                "overclaim_flags": [],
                "contradiction_notes": [],
                "figure_placements": [],
                "status": "audited",
                "uncited_load_bearing": [],
                "revision_history": [],
            }
        )
        packets.append(_packet_row(section_id, title))
        blueprint_sections.append({"section_id": section_id, "title": title})
    return {
        "schema_version": "full_review.section_drafts.v1",
        "blueprint": {"sections": blueprint_sections},
        "section_drafts": drafts,
        "material_packets": packets,
        "full_review_english": "",
        "quality_summary": {},
    }


class M4PatchProvider:
    """Offline provider: deterministic roles + injected commander patches."""

    def __init__(
        self,
        patch_builder: Callable[[dict[str, Any]], list[dict[str, Any]]]
        | None = None,
        *,
        raise_on_commander: bool = False,
    ) -> None:
        self.patch_builder = patch_builder
        self.raise_on_commander = raise_on_commander
        self.delegate = DeterministicRoleProvider()
        self.calls: list[dict[str, Any]] = []

    def __call__(self, role: str, payload: dict[str, Any]) -> Any:
        self.calls.append({"role": role, "payload": payload})
        if role != "commander_synthesis":
            return self.delegate(role, payload)
        if self.raise_on_commander:
            raise RuntimeError("live Qwen unavailable")
        context = payload.get("canonical_context") or {}
        sections = [
            str(section.get("section_id") or "")
            for section in (context.get("sections") or [])
            if isinstance(section, dict)
        ]
        patches = self.patch_builder(context) if self.patch_builder else []
        return {
            "manuscript_diagnosis": "Offline M4 test diagnosis.",
            "proposed_section_order": [
                {"section_id": section_id, "reason": "keep"}
                for section_id in sections
            ],
            "section_decisions": [
                {
                    "section_id": section_id,
                    "decision": "retain",
                    "rationale": "offline test",
                }
                for section_id in sections
            ],
            "cross_section_conflicts": [],
            "missing_axes": [],
            "structure_gaps": [],
            "repeated_paper_role_audit": [],
            "visual_work_orders": [],
            "retrieval_gap_proposals": [],
            "affected_section_ids": sections,
            "next_execution_stages": [],
            "retained_advisory_issues": [],
            "proposed_patch_set": patches,
            "read_only_declaration": {
                "chapter_text_changed": False,
                "retrieval_launched": False,
                "note": "offline test",
            },
        }


def _first_block_patch(
    context: dict[str, Any],
    *,
    operation_type: str,
    after_text: str = "",
    stale_hash: bool = False,
    block_index: int = 0,
    ownership_before: list[str] | None = None,
    ownership_after: list[str] | None = None,
) -> list[dict[str, Any]]:
    blocks = context.get("patch_blocks") or {}
    if not blocks:
        return []
    first_section = (
        str((context.get("sections") or [{}])[0].get("section_id") or "")
    )
    ordered = [
        str(paragraph.get("canonical_id") or "")
        for section in (context.get("sections") or [])
        for paragraph in (section.get("paragraphs") or [])
        if isinstance(section, dict) and isinstance(paragraph, dict)
    ]
    section_blocks = [
        block_id
        for block_id in ordered
        if blocks[block_id]["section_id"] == first_section
    ]
    if not section_blocks:
        section_blocks = sorted(blocks)
    target = section_blocks[min(block_index, len(section_blocks) - 1)]
    block = blocks[target]
    section_ids = [
        str(section.get("section_id") or "")
        for section in (context.get("sections") or [])
        if isinstance(section, dict)
    ]
    destination = section_ids[1] if len(section_ids) > 1 else block["section_id"]
    base_hash = "0" * 64 if stale_hash else block["hash"]
    ownership_before = (
        [block["section_id"]]
        if ownership_before is None
        else ownership_before
    )
    ownership_after = (
        [destination] if ownership_after is None else ownership_after
    )
    return [
        {
            "patch_id": "M4-P01",
            "operation_type": operation_type,
            "target_section_id": block["section_id"],
            "target_block_id": target,
            "source_section_id": block["section_id"],
            "source_block_id": target,
            "destination_section_id": destination,
            "base_hash": base_hash,
            "source_hash": block["hash"],
            "block_text_after": after_text,
            "reason": "Offline M4 test patch.",
            "finding_ids": ["GMC-SS-001"],
            "claims_before": list(block.get("contract_claim_ids") or []),
            "claims_after": list(block.get("contract_claim_ids") or []),
            "evidence_before": [],
            "evidence_after": [],
            "ownership_before": ownership_before,
            "ownership_after": ownership_after,
            "claim_strength_change": "none",
            "citation_change": "none",
            "collateral_block_ids": [],
            "invariants": [
                "no_scientific_meaning_change",
                "preserve_sibling_boundaries",
            ],
            "risk": "none" if operation_type == "move_block" else "medium",
            "approval_required": operation_type != "move_block",
        }
    ]


def _original_texts(bundle: dict[str, Any]) -> dict[str, str]:
    return {
        str(row.get("section_id") or ""): str(row.get("english_text") or "")
        for row in bundle.get("section_drafts") or []
    }


def test_s15_applies_safe_deterministic_move_patch(tmp_path: Path) -> None:
    bundle = _draft_bundle(
        s01_text=(
            "# First Chapter\n\n"
            "Opening paragraph of S01 with [REF:paper_S01].\n\n"
            "Second Chapter analysis paragraph.\n"
        )
    )
    originals = _original_texts(bundle)
    provider = M4PatchProvider(
        lambda context: _first_block_patch(
            context, operation_type="move_block", block_index=2
        )
    )
    result = edit_cross_section(
        bundle,
        {},
        real_llm=False,
        m4_role_provider=provider,
        m4_snapshot_dir=tmp_path / "snapshot",
        m4_diagnostics_dir=tmp_path / "diagnostics",
        m4_proposal_path=tmp_path / "m4_patch_proposal.json",
    )
    assert result["m4_apply_status"] == "applied"
    assert result["changed_section_ids"] == ["S01", "S02"]
    contract = result["m4_contract"]
    assert contract["status"] == "applied"
    assert contract["base_snapshot_hash"] != contract["post_snapshot_hash"]
    assert contract["validation"]["status"] == "valid"
    move_report = contract["validation"]["patch_reports"][0]
    assert move_report["ownership_compliance"] == "proven"
    assert move_report["boundary_compliance"] == "proven"
    assert contract["apply_report"]["applied_patches"][0][
        "operation_type"
    ] == "move_block"
    assert contract["post_apply_citation_audit"] is not None
    assert contract["post_apply_claim_evidence_audit"]["status"] == "passed"
    assert (tmp_path / "snapshot" / "pre_edit_bundle.json").is_file()
    moved_text = "Second Chapter analysis paragraph."
    output_by_id = {
        str(row.get("section_id") or ""): str(row.get("english_text") or "")
        for row in result["section_drafts"]
    }
    assert moved_text in output_by_id["S02"]
    assert moved_text not in output_by_id["S01"]
    assert output_by_id["S01"] != originals["S01"]
    assert output_by_id["S02"] != originals["S02"]
    assert provider.calls[0]["role"] == "structure_strategist"
    assert provider.calls[-1]["role"] == "commander_synthesis"


def test_s15_rejects_stale_hash_patch_and_keeps_original(
    tmp_path: Path,
) -> None:
    bundle = _draft_bundle()
    originals = _original_texts(bundle)
    provider = M4PatchProvider(
        lambda context: _first_block_patch(
            context, operation_type="move_block", stale_hash=True
        )
    )
    result = edit_cross_section(
        bundle,
        {},
        real_llm=False,
        m4_role_provider=provider,
        m4_snapshot_dir=tmp_path / "snapshot",
        m4_diagnostics_dir=tmp_path / "diagnostics",
        m4_proposal_path=tmp_path / "m4_patch_proposal.json",
    )
    assert result["m4_apply_status"] == "rejected"
    assert result["changed_section_ids"] == []
    contract = result["m4_contract"]
    assert any(
        "stale base_hash" in error for error in contract["validation"]["errors"]
    )
    assert contract["post_snapshot_hash"] == contract["base_snapshot_hash"]
    output_by_id = {
        str(row.get("section_id") or ""): str(row.get("english_text") or "")
        for row in result["section_drafts"]
    }
    assert output_by_id == originals
    assert contract["original_bundle"]["recoverable"] is True


def test_s15_semantic_patch_awaits_then_declines_then_applies(
    tmp_path: Path,
) -> None:
    bundle = _draft_bundle()
    originals = _original_texts(bundle)
    provider = M4PatchProvider(
        lambda context: _first_block_patch(
            context,
            operation_type="rewrite_transition",
            after_text="Approved transition rewrite text.",
            block_index=2,
        )
    )
    kwargs = {
        "real_llm": False,
        "m4_role_provider": provider,
        "m4_snapshot_dir": tmp_path / "snapshot",
        "m4_diagnostics_dir": tmp_path / "diagnostics",
        "m4_proposal_path": tmp_path / "m4_patch_proposal.json",
    }
    first = edit_cross_section(bundle, {}, **kwargs)
    assert first["m4_apply_status"] == "awaiting_approval"
    assert first["changed_section_ids"] == []
    assert (
        first["m4_contract"]["validation"]["awaiting_patches"][0]["patch_id"]
        == "M4-P01"
    )
    call_count_after_first = len(provider.calls)

    declined = edit_cross_section(bundle, {}, approvals={"M4-P01": "declined"}, **kwargs)
    assert declined["m4_apply_status"] == "noop"
    assert declined["changed_section_ids"] == []
    assert declined["m4_contract"]["apply_report"]["byte_identical"] is True
    assert len(provider.calls) == call_count_after_first, (
        "resumed proposal must reuse the frozen proposal without re-calling Qwen"
    )
    declined_output = {
        str(row.get("section_id") or ""): str(row.get("english_text") or "")
        for row in declined["section_drafts"]
    }
    assert declined_output == originals

    approved = edit_cross_section(
        bundle, {}, approvals={"M4-P01": "approved"}, **kwargs
    )
    assert approved["m4_apply_status"] == "applied"
    approved_text = next(
        row["english_text"]
        for row in approved["section_drafts"]
        if row["section_id"] == "S01"
    )
    assert "Approved transition rewrite text." in approved_text
    assert approved["m4_contract"]["post_apply_citation_audit"] is not None


def test_s15_failed_qwen_keeps_original_and_records_diagnostics(
    tmp_path: Path,
) -> None:
    bundle = _draft_bundle()
    originals = _original_texts(bundle)
    provider = M4PatchProvider(raise_on_commander=True)
    result = edit_cross_section(
        bundle,
        {},
        real_llm=True,
        m4_role_provider=provider,
        m4_snapshot_dir=tmp_path / "snapshot",
        m4_diagnostics_dir=tmp_path / "diagnostics",
        m4_proposal_path=tmp_path / "m4_patch_proposal.json",
    )
    assert result["m4_apply_status"] == "failed_qwen"
    assert result["changed_section_ids"] == []
    contract = result["m4_contract"]
    assert contract["commander"]["status"] != "completed"
    assert "Qwen" in contract["commander"]["error"]
    assert contract["post_snapshot_hash"] == contract["base_snapshot_hash"]
    output_by_id = {
        str(row.get("section_id") or ""): str(row.get("english_text") or "")
        for row in result["section_drafts"]
    }
    assert output_by_id == originals
    assert contract["post_apply_citation_audit"] is None
    assert (tmp_path / "diagnostics" / "run_state.json").is_file()


def test_orchestrator_s15_awaits_approval_then_post_apply_failure_stops(
    tmp_path: Path,
) -> None:
    orch = FullReviewOrchestrator(
        output_dir=tmp_path / "out", real_llm=False
    )
    orch._m4_role_provider = M4PatchProvider(
        lambda context: _first_block_patch(
            context, operation_type="move_block"
        )
    )
    state = FullReviewState.new(
        user_query="Radiative cooling materials review.", domain="optical_science"
    )
    for _ in range(10):
        state = orch.run(state)
        if (
            state.status == "needs_human"
            and state.stages["S15_cross_section_edit"].status == "needs_human"
        ):
            break
    assert (
        state.stages["S15_cross_section_edit"].status == "needs_human"
    ), "S15 must pause for M4 patch approval"
    ref_dir = Path(state.cross_section_edit_ref.path).parent
    approvals_path = ref_dir / "m4_patch_approvals.json"
    approvals_path.write_text(
        json.dumps(
            {
                "schema_version": "full_review.m4_patch_approvals.v1",
                "decisions": [
                    {
                        "patch_id": "M4-P01",
                        "decision": "approved",
                        "approver": "human",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    for _ in range(10):
        state = orch.run(state)
        if state.status != "needs_human":
            break
    assert state.status == "failed"
    s15 = state.stages["S15_cross_section_edit"]
    assert s15.status == "failed"
    assert s15.stop_reason == "m4_post_apply_audit_failed"
    assert (
        state.stages["S16_supervisor_review"].status == "pending"
    ), "S16 must never receive candidate text after a post-apply audit failure"
    # The stage ref still points at the needs-human artifact (frozen original,
    # awaiting approval); the rejected candidate artifact is never attached.
    awaiting_bundle = json.loads(
        Path(state.cross_section_edit_ref.path).read_text(encoding="utf-8")
    )
    assert awaiting_bundle["m4_apply_status"] == "awaiting_approval"
    candidate_bundle = json.loads(
        (
            tmp_path
            / "out"
            / "S15_cross_section_edit"
            / "attempt_2"
            / "cross_section_edit.json"
        ).read_text(encoding="utf-8")
    )
    assert candidate_bundle["m4_apply_status"] == "rejected"
    assert (
        candidate_bundle["m4_contract"]["rollback_report"]["status"]
        == "rolled_back"
    )
    assert (
        candidate_bundle["m4_contract"]["stop_reason"]
        == "m4_post_apply_audit_failed"
    )
    original_texts = {
        str(row.get("section_id") or ""): str(row.get("english_text") or "")
        for row in awaiting_bundle["section_drafts"]
    }
    candidate_texts = {
        str(row.get("section_id") or ""): str(row.get("english_text") or "")
        for row in candidate_bundle["section_drafts"]
    }
    assert candidate_texts == original_texts, (
        "the rejected candidate artifact must carry the restored frozen "
        "original, never candidate text"
    )


def _rollback_assertions(
    result: dict[str, Any],
    originals: dict[str, str],
    *,
    expected_failures: list[str],
) -> dict[str, Any]:
    assert result["m4_apply_status"] == "rejected"
    assert result["changed_section_ids"] == []
    contract = result["m4_contract"]
    assert contract["status"] == "rejected"
    assert contract["stop_reason"] == "m4_post_apply_audit_failed"
    assert contract["post_snapshot_hash"] == contract["base_snapshot_hash"]
    rollback = contract["rollback_report"]
    assert rollback["status"] == "rolled_back"
    assert rollback["attempted_patch_ids"] == ["M4-P01"]
    assert rollback["candidate_post_snapshot_hash"] != rollback[
        "restored_base_snapshot_hash"
    ]
    assert rollback["byte_identical_restoration"] is True
    assert rollback["recoverable_snapshot_path"]
    assert rollback["audit_failures"] == expected_failures
    assert contract["failed_candidate_audits"] is not None
    output_by_id = {
        str(row.get("section_id") or ""): str(row.get("english_text") or "")
        for row in result["section_drafts"]
    }
    assert output_by_id == originals
    return contract


def test_s15_rolls_back_on_citation_audit_failure(
    tmp_path: Path, monkeypatch
) -> None:
    import optomind_research.full_review_production as production_module

    bundle = _draft_bundle(
        s01_text=(
            "# First Chapter\n\n"
            "Opening paragraph of S01 with [REF:paper_S01].\n\n"
            "Second Chapter analysis paragraph.\n"
        )
    )
    originals = _original_texts(bundle)
    provider = M4PatchProvider(
        lambda context: _first_block_patch(
            context, operation_type="move_block", block_index=2
        )
    )

    def failing_citation_audit(*_args, **_kwargs) -> dict[str, Any]:
        return {
            "invalid_citation_count": 1,
            "uncited_load_bearing_claim_count": 0,
            "quality_judge_failure_count": 0,
            "formal_ready_section_count": 0,
            "citation_ready_section_count": 0,
            "citation_audits": [],
        }

    monkeypatch.setattr(
        production_module, "audit_citations", failing_citation_audit
    )
    result = edit_cross_section(
        bundle,
        {},
        real_llm=False,
        m4_role_provider=provider,
        m4_snapshot_dir=tmp_path / "snapshot",
        m4_diagnostics_dir=tmp_path / "diagnostics",
        m4_proposal_path=tmp_path / "m4_patch_proposal.json",
    )
    contract = _rollback_assertions(
        result, originals, expected_failures=["citation_audit"]
    )
    assert (
        contract["failed_candidate_audits"]["post_apply_citation_audit"][
            "invalid_citation_count"
        ]
        == 1
    )
    assert contract["rollback_report"]["candidate_audit_reports_preserved"] is True


def test_s15_rolls_back_on_ledger_failure(tmp_path: Path) -> None:
    bundle = _draft_bundle(
        s01_text=(
            "# First Chapter\n\n"
            "Opening paragraph of S01 with [REF:paper_S01].\n\n"
            "Second Chapter paragraph with [REF:paper_unknown].\n"
        )
    )
    originals = _original_texts(bundle)
    provider = M4PatchProvider(
        lambda context: _first_block_patch(
            context, operation_type="move_block", block_index=2
        )
    )
    result = edit_cross_section(
        bundle,
        {},
        real_llm=False,
        m4_role_provider=provider,
        m4_snapshot_dir=tmp_path / "snapshot",
        m4_diagnostics_dir=tmp_path / "diagnostics",
        m4_proposal_path=tmp_path / "m4_patch_proposal.json",
    )
    contract = _rollback_assertions(
        result,
        originals,
        expected_failures=["claim_evidence_ledger"],
    )
    ledger = contract["failed_candidate_audits"][
        "post_apply_claim_evidence_audit"
    ]
    assert ledger["status"] == "attention"
    assert any("paper_unknown" in issue for issue in ledger["issues"])


def test_s15_rolls_back_on_continuity_failure(tmp_path: Path) -> None:
    bundle = _draft_bundle(
        s01_text=(
            "# First Chapter\n\n"
            "Opening paragraph of S01 with [REF:paper_S01].\n\n"
            "Second paragraph of S01.\n\n"
            "In summary, the evidence establishes the Second Chapter "
            "mechanism.\n"
        )
    )
    originals = _original_texts(bundle)
    provider = M4PatchProvider(
        lambda context: _first_block_patch(
            context, operation_type="move_block", block_index=3
        )
    )
    result = edit_cross_section(
        bundle,
        {},
        real_llm=False,
        m4_role_provider=provider,
        m4_snapshot_dir=tmp_path / "snapshot",
        m4_diagnostics_dir=tmp_path / "diagnostics",
        m4_proposal_path=tmp_path / "m4_patch_proposal.json",
    )
    contract = _rollback_assertions(
        result, originals, expected_failures=["continuity"]
    )
    continuity = contract["failed_candidate_audits"][
        "post_apply_continuity_audit"
    ]
    assert continuity["passed"] is False
    assert any(
        row.get("issue_type") == "body_mini_conclusion"
        for row in continuity["findings"]
    )


def test_s15_move_ownership_mismatch_rejected_even_with_approval(
    tmp_path: Path,
) -> None:
    bundle = _draft_bundle()
    originals = _original_texts(bundle)
    provider = M4PatchProvider(
        lambda context: _first_block_patch(
            context,
            operation_type="move_block",
            ownership_before=["S02"],
        )
    )
    result = edit_cross_section(
        bundle,
        {},
        real_llm=False,
        approvals={"M4-P01": "approved"},
        m4_role_provider=provider,
        m4_snapshot_dir=tmp_path / "snapshot",
        m4_diagnostics_dir=tmp_path / "diagnostics",
        m4_proposal_path=tmp_path / "m4_patch_proposal.json",
    )
    assert result["m4_apply_status"] == "rejected"
    assert result["changed_section_ids"] == []
    contract = result["m4_contract"]
    assert any(
        "ownership_before must be exactly" in error
        for error in contract["validation"]["errors"]
    )
    assert contract["post_snapshot_hash"] == contract["base_snapshot_hash"]
    output_by_id = {
        str(row.get("section_id") or ""): str(row.get("english_text") or "")
        for row in result["section_drafts"]
    }
    assert output_by_id == originals


def test_s15_move_destination_must_not_cover_contradiction_rejected(
    tmp_path: Path,
) -> None:
    bundle = _draft_bundle()
    # Destination S02 forbids the moved block's content.
    for packet in bundle["material_packets"]:
        if packet["section_id"] == "S02":
            packet["section_contract"]["must_not_cover"] = ["First Chapter"]
    originals = _original_texts(bundle)
    provider = M4PatchProvider(
        lambda context: _first_block_patch(context, operation_type="move_block")
    )
    result = edit_cross_section(
        bundle,
        {},
        real_llm=False,
        approvals={"M4-P01": "approved"},
        m4_role_provider=provider,
        m4_snapshot_dir=tmp_path / "snapshot",
        m4_diagnostics_dir=tmp_path / "diagnostics",
        m4_proposal_path=tmp_path / "m4_patch_proposal.json",
    )
    assert result["m4_apply_status"] == "rejected"
    assert result["changed_section_ids"] == []
    contract = result["m4_contract"]
    assert any(
        "destination boundary contradiction" in error
        for error in contract["validation"]["errors"]
    )
    assert contract["validation"]["patch_reports"][0][
        "boundary_compliance"
    ] == "failed"
    output_by_id = {
        str(row.get("section_id") or ""): str(row.get("english_text") or "")
        for row in result["section_drafts"]
    }
    assert output_by_id == originals
