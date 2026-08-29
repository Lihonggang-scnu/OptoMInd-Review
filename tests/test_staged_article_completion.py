"""Focused tests for the staged full-manuscript completion increment."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping

import pytest

import scripts.run_staged_article_completion as cli
from llm import qwen_chat_client
from optomind_research.runtime.article_completion_runner import (
    run_staged_article_completion as runner_reexport,
)
from optomind_research.runtime.article_completion_schemas import (
    STAGE_ORDER as RE_EXPORTED_STAGE_ORDER,
    StagedArticleCompletionState as ReExportedState,
)
from optomind_research.runtime.global_manuscript_commander import (
    STAGED_AUTHORITY_SCHEMA_VERSION,
    build_staged_structure_authority,
)
from optomind_research.runtime.staged_article_completion import (
    AbstractDraft,
    AbstractWorkplan,
    BoundedPatchProposal,
    CommanderStructuralAuthority,
    ConclusionDraft,
    ConclusionWorkplan,
    EditorialRevisionAudit,
    EditorialWorkItem,
    IntroductionDraft,
    IntroductionWorkplan,
    ManuscriptReviewFinding,
    ManuscriptReviewReport,
    MultiReviewerReport,
    PatchProposalSet,
    QwenMultiReviewerProvider,
    QwenEditorialRevisionProvider,
    QwenStagedProvider,
    ReviewerRole,
    SCHEMA_VERSION,
    SEMANTIC_PATCH_OPERATIONS,
    STATE_JSON,
    STAGE_ORDER,
    StagedArticleCompletionState,
    StagedCompletionError,
    StagedStageState,
    aggregate_multi_reviewer_report,
    assemble_revised_manuscript,
    build_commander_structural_authority,
    findings_are_fail_open,
    is_blocking_finding,
    make_editorial_revision_qwen_provider,
    make_multi_reviewer_qwen_provider,
    make_qwen_stage_provider,
    normalize_patch_proposal,
    parse_fenced_json,
    patch_set_requires_approval,
    plan_editorial_work_items,
    plan_editorial_work_items_with_provenance,
    proposal_requires_approval,
    ref_markers_preserved,
    run_staged_article_completion,
    validate_claim_evidence_invariant_preserved,
    verifier_accepts,
    LiteratureReviewPresentationIR,
    TitleCandidate,
    ReviewTitlePlan,
    extract_presentation_ir,
    plan_review_titles,
    FRONT_MATTER_STAGES,
    PRESENTATION_IR_SCHEMA,
    TITLE_PLAN_SCHEMA,
)


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory."""
    root = (
        Path(__file__).resolve().parents[1]
        / ".pytest-basetemp-staged-completion"
    )
    root.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)[:40]
    path = root / f"{safe_name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def _work_order() -> dict[str, Any]:
    return {
        "schema_version": "optomind.global_manuscript_commander.work_order.v2",
        "status": "completed",
        "fingerprint": "work-order-fp",
        "proposed_section_order": [
            {"section_id": "S01", "position": 0, "rationale": "opens"},
            "S02",
        ],
        "section_decisions": [
            {"section_id": "S01", "responsibility": "mechanism"}
        ],
        "repeated_paper_role_audit": [
            {"section_ids": ["S01", "S02"], "duplication_type": "shared_paper"}
        ],
        "missing_axes": [{"axis_id": "F09", "description": "axis"}],
        "structure_gaps": [
            {
                "gap_type": "role_overlap",
                "section_id": "S01",
                "proposal": "split responsibility",
            }
        ],
        "visual_work_orders": [
            {"figure_id": "fig-1", "work_order": "render field map"}
        ],
        "proposed_patch_set": [
            {
                "patch_id": "patch-semantic",
                "operation": "evidence_change",
                "target": "S01",
            }
        ],
        "read_only_declaration": {
            "chapter_text_changed": False,
            "retrieval_launched": False,
        },
    }


def test_contracts_importable_and_validate_representative_data() -> None:
    authority = CommanderStructuralAuthority(
        section_order=[{"section_id": "S01", "position": 0}],
        section_responsibilities=[
            {"section_id": "S01", "responsibility": "mechanism"}
        ],
        structure_gaps=[{"gap_type": "role_overlap", "proposal": "p"}],
        approval_required_for=["patch-semantic"],
    )
    assert authority.claim_evidence_invariant == "preserved"
    assert authority.read_only is True

    conclusion = ConclusionWorkplan(derived_from=["commander_structure"])
    draft = ConclusionDraft(text="conclusion", workplan_fingerprint="w")
    introduction = IntroductionWorkplan(
        promises_derived_from=["conclusion"],
        retrieval_proposals=["query for missing background"],
    )
    abstract = AbstractDraft(text="abstract")
    finding = ManuscriptReviewFinding(
        finding_id="f1",
        issue_key="flow-gap",
        issue_type="role_overlap",
        target_ids=["S01", "S02"],
        dimension="continuity",
        severity="advisory",
        consensus="none",
        statement="s",
    )
    patch = BoundedPatchProposal(
        patch_id="p1",
        operation="move_block",
        target="S01",
        approval_required=False,
    )
    for model in (
        authority,
        conclusion,
        draft,
        introduction,
        AbstractWorkplan(compresses=["conclusion"]),
        abstract,
        finding,
        ManuscriptReviewReport(findings=[finding]),
        patch,
        PatchProposalSet(proposals=[patch]),
        StagedStageState(stage="conclusion", status="completed"),
    ):
        dumped = model.model_dump(mode="json")
        assert model.model_validate(dumped) == model

    assert RE_EXPORTED_STAGE_ORDER == STAGE_ORDER
    assert ReExportedState is StagedArticleCompletionState
    assert runner_reexport is run_staged_article_completion
    assert introduction.retrieval_proposals == [
        "query for missing background"
    ]
    assert finding.issue_key == "flow-gap"
    assert finding.target_ids == ["S01", "S02"]


def test_offline_runner_writes_and_reloads_artifacts(tmp_path: Path) -> None:
    state = run_staged_article_completion(
        work_dir=tmp_path / "out",
        inputs={"section_order": ["S01", "S02"]},
        metadata={"topic": "pin"},
        run_id="run-1",
    )

    assert state.schema_version == SCHEMA_VERSION
    assert state.stage_order == list(STAGE_ORDER)
    assert state.status == "completed"
    assert state.all_completed is True
    assert len(state.stages) == len(STAGE_ORDER)
    for stage in STAGE_ORDER:
        record = state.stages[stage]
        assert record.status == "completed"
        assert record.fingerprint
        assert record.artifact_path
        assert Path(record.artifact_path).is_file()
        assert json.loads(Path(record.artifact_path).read_text(encoding="utf-8"))[
            "stage"
        ] == stage

    state_path = tmp_path / "out" / "staged_article_completion_state.json"
    reloaded = StagedArticleCompletionState.model_validate(
        json.loads(state_path.read_text(encoding="utf-8"))
    )
    assert reloaded == state
    assert reloaded.run_fingerprint == state.run_fingerprint

    order = state.stage_order
    assert order.index("conclusion") < order.index("introduction")
    assert order.index("introduction") < order.index("abstract")
    assert order.index("abstract") < order.index("whole_manuscript_review")
    assert order.index("whole_manuscript_review") < order.index(
        "bounded_patch_proposals"
    )
    assert order.index("bounded_patch_proposals") < order.index(
        "visual_remount"
    )
    assert order.index("visual_remount") < order.index("assembly_preflight")


def test_resume_noop_when_fingerprints_unchanged(tmp_path: Path) -> None:
    calls: dict[str, int] = {"conclusion": 0}

    def conclusion_provider(
        _stage_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        calls["conclusion"] += 1
        return {
            "workplan": ConclusionWorkplan(
                derived_from=["commander_structure"], outline=["x"]
            ).model_dump(mode="json"),
            "draft": ConclusionDraft(text="draft").model_dump(mode="json"),
            "status": "offline",
        }

    providers = {"conclusion": conclusion_provider}
    first = run_staged_article_completion(
        work_dir=tmp_path / "out",
        inputs={"section_order": ["S01"]},
        stage_providers=providers,
        run_id="resume-1",
    )
    assert calls["conclusion"] == 1
    conclusion_path = Path(first.stages["conclusion"].artifact_path)
    before = conclusion_path.read_bytes()

    second = run_staged_article_completion(
        work_dir=tmp_path / "out",
        inputs={"section_order": ["S01"]},
        stage_providers=providers,
        resume=True,
        run_id="resume-1",
    )
    assert calls["conclusion"] == 1  # no second provider payment
    assert second.stages["conclusion"].status == "noop"
    assert conclusion_path.read_bytes() == before


def test_resume_refuses_changed_inputs(tmp_path: Path) -> None:
    calls: dict[str, int] = {"conclusion": 0}

    def conclusion_provider(
        _stage_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        calls["conclusion"] += 1
        return {
            "workplan": ConclusionWorkplan(
                derived_from=["commander_structure"]
            ).model_dump(mode="json"),
            "draft": ConclusionDraft(text="x").model_dump(mode="json"),
            "status": "offline",
        }

    run_staged_article_completion(
        work_dir=tmp_path / "out",
        inputs={"section_order": ["S01"]},
        stage_providers={"conclusion": conclusion_provider},
        run_id="same",
    )
    with pytest.raises(StagedCompletionError, match="fingerprint changed"):
        run_staged_article_completion(
            work_dir=tmp_path / "out",
            inputs={"section_order": ["S01", "S02"]},
            stage_providers={"conclusion": conclusion_provider},
            resume=True,
            run_id="same",
        )
    assert calls["conclusion"] == 1  # refusal happened before any provider call


def test_fail_open_advisory_findings() -> None:
    advisory = ManuscriptReviewFinding(
        finding_id="a",
        severity="advisory",
        consensus="majority",
        statement="s",
    )
    minor = ManuscriptReviewFinding(
        finding_id="b",
        severity="minor",
        consensus="none",
        statement="s",
    )
    assert is_blocking_finding(advisory) is False
    assert is_blocking_finding(minor) is False
    assert findings_are_fail_open([advisory, minor]) is True

    critical_consensus = ManuscriptReviewFinding(
        finding_id="c",
        severity="critical",
        consensus="consensus",
        statement="s",
        blocking=True,
    )
    assert is_blocking_finding(critical_consensus) is True
    assert findings_are_fail_open([advisory, critical_consensus]) is False


def test_review_fail_open_severity_consensus_matrix() -> None:
    cases = [
        ("advisory", "consensus", False, False),
        ("major", "majority", False, False),
        ("critical", "majority", False, False),
        # split-severity critical group: consensus on the issue but not on
        # severity -> locally nonblocking/advisory.
        ("critical", "consensus", False, False),
        ("critical", "consensus", True, True),
    ]
    for severity, consensus, blocking, expected in cases:
        finding = ManuscriptReviewFinding(
            finding_id=f"{severity}-{consensus}-{blocking}",
            severity=severity,
            consensus=consensus,
            statement="s",
            blocking=blocking,
        )
        assert is_blocking_finding(finding) is expected

    critical_majority = ManuscriptReviewFinding(
        finding_id="critical-majority",
        severity="critical",
        consensus="majority",
        statement="s",
    )
    assert findings_are_fail_open([critical_majority]) is True
    critical_consensus = ManuscriptReviewFinding(
        finding_id="critical-consensus",
        severity="critical",
        consensus="consensus",
        statement="s",
        blocking=True,
    )
    assert findings_are_fail_open([critical_consensus]) is False


def test_commander_authority_compatibility_mappings() -> None:
    work_order = {
        "schema_version": "optomind.global_manuscript_commander.work_order.v2",
        "status": "completed",
        "fingerprint": "compat",
        "section_decisions": [
            {
                "section_id": "S01",
                "decision": "mechanism section",
                "rationale": "because",
            }
        ],
        "cross_section_conflicts": [
            {
                "sections": ["S02", "S03"],
                "conflict_type": "overlap",
            }
        ],
        "proposed_patch_set": [
            {
                "patch_id": "m4-semantic",
                "operation_type": "evidence_change",
                "target": "S01",
            },
            {
                "patch_id": "m4-move",
                "operation_type": "move_block",
                "target": "S01",
            },
        ],
        "read_only_declaration": {
            "chapter_text_changed": False,
            "retrieval_launched": False,
        },
    }
    authority = build_commander_structural_authority(work_order)
    assert authority.section_responsibilities[0].responsibility == (
        "mechanism section | because"
    )
    assert "mechanism section" in authority.section_responsibilities[
        0
    ].responsibility
    assert "because" in authority.section_responsibilities[0].responsibility
    assert authority.duplication[0].section_ids == ["S02", "S03"]
    assert authority.duplication[0].duplication_type == "overlap"
    assert authority.approval_required_for == ["m4-semantic"]
    assert "m4-move" not in authority.approval_required_for


def test_m4_semantic_vs_move_approval_using_operation_type() -> None:
    move = normalize_patch_proposal(
        {
            "patch_id": "move",
            "operation_type": "move_block",
            "target": "S01",
        }
    )
    assert move.approval_required is False

    delete = normalize_patch_proposal(
        {
            "patch_id": "delete",
            "operation_type": "delete_block",
            "target": "S01",
        }
    )
    assert delete.approval_required is True

    approval_ids = patch_set_requires_approval(
        [
            {
                "patch_id": "move",
                "operation_type": "move_block",
                "target": "S01",
            },
            {
                "patch_id": "delete",
                "operation_type": "delete_block",
                "target": "S01",
            },
            {
                "patch_id": "claim",
                "operation_type": "claim_strength_change",
                "target": "S01",
            },
        ]
    )
    assert approval_ids == ["claim", "delete"]


def test_no_silent_claim_evidence_changes(tmp_path: Path) -> None:
    with pytest.raises(StagedCompletionError, match="claim_text_changes"):
        validate_claim_evidence_invariant_preserved(
            {"claim_text_changes": []}
        )

    work_order = _work_order()
    work_order["read_only_declaration"]["chapter_text_changed"] = True
    with pytest.raises(StagedCompletionError, match="chapter text changes"):
        build_commander_structural_authority(work_order)

    bad_work_dir = tmp_path / "bad-invariant"
    with pytest.raises(StagedCompletionError, match="claim/evidence invariant"):
        run_staged_article_completion(
            work_dir=bad_work_dir,
            inputs={
                "commander_work_order": {
                    "read_only_declaration": {"chapter_text_changed": False},
                    "claim_text_changes": ["bad"],
                }
            },
        )
    # The runner persists state after completed earlier stages, but the
    # failing commander stage must not leave an artifact.
    assert not (bad_work_dir / "staged_commander_structure.json").exists()


def test_semantic_patch_approval_required(tmp_path: Path) -> None:
    raw = {
        "patch_id": "p9",
        "operation": "merge_blocks",
        "target": "S01",
        "claim_text_change": True,
    }
    proposal = normalize_patch_proposal(raw)
    assert proposal.approval_required is True
    assert "p9" in patch_set_requires_approval([raw])

    def patch_provider(
        _stage_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        return PatchProposalSet(
            proposals=[
                BoundedPatchProposal(
                    patch_id="p9",
                    operation="merge_blocks",
                    target="S01",
                    claim_text_change=True,
                )
            ]
        ).model_dump(mode="json")

    state = run_staged_article_completion(
        work_dir=tmp_path / "out",
        stage_providers={"bounded_patch_proposals": patch_provider},
        run_id="approval",
    )
    stage = state.stages["bounded_patch_proposals"]
    assert stage.status == "awaiting_approval"
    assert stage.approval_required is True
    assert state.status == "awaiting_approval"
    assert state.awaiting_approval_stages == ["bounded_patch_proposals"]
    assert state.all_completed is False


def test_pure_editorial_rewrite_transition_does_not_require_approval(
    tmp_path: Path,
) -> None:
    pure_editorial = {
        "patch_id": "P-1",
        "operation": "rewrite_transition",
        "target": "S01-B001",
        "rationale": "smooth rhetoric only",
        "claim_text_change": False,
        "evidence_change": False,
    }
    proposal = normalize_patch_proposal(pure_editorial)
    assert proposal.approval_required is False
    assert proposal_requires_approval(pure_editorial) is False
    assert patch_set_requires_approval([pure_editorial]) == []

    # Explicit approval or any claim/evidence flag still gates.
    assert proposal_requires_approval(
        {**pure_editorial, "approval_required": True}
    ) is True
    assert proposal_requires_approval(
        {**pure_editorial, "claim_text_change": True}
    ) is True
    assert proposal_requires_approval(
        {**pure_editorial, "evidence_change": True}
    ) is True

    # Inherently semantic operations always gate, even with explicit false
    # claim/evidence flags.
    for operation in (
        "delete_block",
        "merge_blocks",
        "ownership_change",
        "claim_strength_change",
        "evidence_change",
    ):
        raw = {
            **pure_editorial,
            "patch_id": operation,
            "operation": operation,
        }
        assert proposal_requires_approval(raw) is True
        assert normalize_patch_proposal(raw).approval_required is True

    def patch_provider(
        _stage_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "proposals": [dict(pure_editorial)],
            "approval_required": False,
        }

    state = run_staged_article_completion(
        work_dir=tmp_path / "out",
        stage_providers={"bounded_patch_proposals": patch_provider},
        run_id="pure-editorial",
    )
    stage = state.stages["bounded_patch_proposals"]
    assert stage.status == "completed"
    assert stage.approval_required is False
    assert state.status == "completed"
    assert state.awaiting_approval_stages == []


def test_resume_recomputes_approval_from_persisted_payload(
    tmp_path: Path,
) -> None:
    pure_editorial = {
        "patch_id": "P-1",
        "operation": "rewrite_transition",
        "target": "S01-B001",
        "claim_text_change": False,
        "evidence_change": False,
    }

    def patch_provider(
        _stage_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {"proposals": [dict(pure_editorial)], "approval_required": False}

    work_dir = tmp_path / "resume-approval"
    run_kwargs = dict(
        work_dir=work_dir,
        stage_providers={"bounded_patch_proposals": patch_provider},
        run_id="resume-pure-editorial",
    )
    first = run_staged_article_completion(**run_kwargs)
    assert first.status == "completed"
    assert first.stages["bounded_patch_proposals"].approval_required is False

    # Simulate artifacts persisted by the pre-fix policy: the pure-editorial
    # stage was wrongly left awaiting approval.
    state_path = work_dir / STATE_JSON
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    stored["status"] = "awaiting_approval"
    stored["stages"]["bounded_patch_proposals"]["status"] = (
        "awaiting_approval"
    )
    stored["stages"]["bounded_patch_proposals"]["approval_required"] = True
    state_path.write_text(
        json.dumps(stored, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    resumed = run_staged_article_completion(**run_kwargs, resume=True)
    stage = resumed.stages["bounded_patch_proposals"]
    assert stage.status == "noop"
    assert stage.approval_required is False
    assert resumed.status == "completed"
    assert resumed.awaiting_approval_stages == []

    # A genuinely semantic persisted payload stays gated on resume.
    work_dir_semantic = tmp_path / "resume-semantic"
    semantic = {
        "patch_id": "P-9",
        "operation": "merge_blocks",
        "target": "S01-B001",
    }

    def semantic_provider(
        _stage_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {"proposals": [dict(semantic)], "approval_required": False}

    run_staged_article_completion(
        work_dir=work_dir_semantic,
        stage_providers={"bounded_patch_proposals": semantic_provider},
        run_id="resume-semantic",
    )
    semantic_state_path = work_dir_semantic / STATE_JSON
    semantic_stored = json.loads(
        semantic_state_path.read_text(encoding="utf-8")
    )
    semantic_stored["stages"]["bounded_patch_proposals"]["status"] = (
        "awaiting_approval"
    )
    semantic_stored["stages"]["bounded_patch_proposals"][
        "approval_required"
    ] = True
    semantic_state_path.write_text(
        json.dumps(semantic_stored, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    semantic_resumed = run_staged_article_completion(
        work_dir=work_dir_semantic,
        stage_providers={"bounded_patch_proposals": semantic_provider},
        run_id="resume-semantic",
        resume=True,
    )
    semantic_stage = semantic_resumed.stages["bounded_patch_proposals"]
    assert semantic_stage.status == "noop"
    assert semantic_stage.approval_required is True
    assert semantic_resumed.status == "awaiting_approval"
    assert semantic_resumed.awaiting_approval_stages == [
        "bounded_patch_proposals"
    ]


def test_commander_authority_extraction_and_staged_helper() -> None:
    authority = build_commander_structural_authority(
        _work_order(), source_work_order_path="outputs/work_order.json"
    )
    assert [entry.section_id for entry in authority.section_order] == [
        "S01",
        "S02",
    ]
    assert authority.section_responsibilities[0].responsibility == "mechanism"
    assert authority.duplication[0].duplication_type == "shared_paper"
    assert authority.missing_axes[0].axis_id == "F09"
    assert authority.structure_gaps[0].gap_type == "role_overlap"
    assert authority.visual_work_orders[0].figure_id == "fig-1"
    assert authority.approval_required_for == ["patch-semantic"]
    assert authority.claim_evidence_invariant == "preserved"
    assert SEMANTIC_PATCH_OPERATIONS == {
        "delete_block",
        "merge_blocks",
        "ownership_change",
        "claim_strength_change",
        "evidence_change",
    }

    staged = build_staged_structure_authority(
        _work_order(), source_work_order_path="outputs/work_order.json"
    )
    assert staged["schema_version"] == STAGED_AUTHORITY_SCHEMA_VERSION
    assert staged["claim_evidence_invariant"] == "preserved"


def test_stage_derived_promises_and_abstract_order(tmp_path: Path) -> None:
    state = run_staged_article_completion(
        work_dir=tmp_path / "out",
        inputs={"section_order": ["S01"]},
        run_id="derived",
    )
    conclusion = state.stages["conclusion"].payload["workplan"]
    assert "commander_structure" in conclusion["derived_from"]
    introduction = state.stages["introduction"].payload["workplan"]
    assert "conclusion" in introduction["promises_derived_from"]
    abstract = state.stages["abstract"].payload["workplan"]
    assert {"conclusion", "introduction"}.issubset(abstract["compresses"])


def test_default_offline_payloads_have_fixed_status_fields(tmp_path: Path) -> None:
    state = run_staged_article_completion(
        work_dir=tmp_path / "out",
        run_id="offline",
    )
    for stage in STAGE_ORDER:
        payload = state.stages[stage].payload
        assert isinstance(payload, dict)
        assert "status" in payload or stage in {
            "commander_structure",
            "bounded_patch_proposals",
        }
    review = state.stages["whole_manuscript_review"].payload["review"]
    assert review["fail_open"] is True
    assert review["findings"] == []


def test_parse_fenced_json_plain_and_fenced() -> None:
    assert parse_fenced_json('{"a": 1}') == {"a": 1}
    assert parse_fenced_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_fenced_json('prefix ```\n{"a": 1}\n``` suffix') == {"a": 1}
    assert parse_fenced_json("not json") is None


def test_qwen_provider_payload_construction_and_runner_usage(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_call_qwen_chat(*args, **kwargs):
        calls.append(
            {
                "agent": args[0],
                "messages": args[1],
                "model_tier": kwargs.get("model_tier"),
                "response_format": kwargs.get("response_format"),
            }
        )
        return {
            "content": json.dumps(
                {
                    "workplan": {
                        "derived_from": ["commander_structure"],
                        "outline": ["synthesize"],
                    },
                    "draft": {"text": "conclusion"},
                    "status": "drafted",
                }
            ),
            "_llm_usage": {
                "agent_name": "StagedConclusionAuthor",
                "model_tier": "c_model",
                "model_name": "qwen3.5-plus",
                "input_tokens": 5,
                "output_tokens": 2,
                "mock_llm": False,
                "success": True,
            },
        }

    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call_qwen_chat)
    provider = make_qwen_stage_provider(
        "conclusion", qwen_call=fake_call_qwen_chat
    )
    stage_input = {
        "stage": "conclusion",
        "inputs": {"section_order": ["S01"]},
        "input_fingerprint": "fp",
    }
    output = provider(stage_input)

    assert len(calls) == 1
    assert calls[0]["agent"] == "StagedConclusionAuthor"
    assert calls[0]["model_tier"] == "c_model"
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "conclusion" in calls[0]["messages"][0]["content"]
    parsed_user = json.loads(calls[0]["messages"][1]["content"])
    assert parsed_user["stage_input"]["stage"] == "conclusion"
    assert output["workplan"]["outline"] == ["synthesize"]
    assert output["status"] == "drafted"
    assert output["_usage"]["input_tokens"] == 5
    assert output["_usage"]["output_tokens"] == 2
    assert output["_usage"]["repair_calls"] == 0

    state = run_staged_article_completion(
        work_dir=tmp_path / "out",
        inputs={"section_order": ["S01"]},
        stage_providers={"conclusion": provider},
        run_id="usage",
    )
    record = state.stages["conclusion"]
    assert record.usage["input_tokens"] == 5
    assert record.usage["output_tokens"] == 2
    assert record.provenance["usage_recorded"] == "True"
    assert "_usage" not in record.payload
    artifact = json.loads(
        (tmp_path / "out" / "staged_conclusion.json").read_text(
            encoding="utf-8"
        )
    )
    assert "_usage" not in artifact["payload"]


def test_qwen_provider_one_bounded_repair_and_failure(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_call_qwen_chat(*args, **kwargs):
        calls.append({"agent": args[0]})
        if len(calls) == 1:
            return {
                "content": "```json\n{broken json}\n```",
                "_llm_usage": {
                    "agent_name": "StagedIntroductionAuthor",
                    "input_tokens": 3,
                    "output_tokens": 1,
                },
            }
        return {
            "content": json.dumps(
                {
                    "workplan": {"promises_derived_from": ["conclusion"]},
                    "draft": {"text": "introduction"},
                    "status": "drafted",
                }
            ),
            "_llm_usage": {
                "agent_name": "StagedIntroductionAuthor",
                "input_tokens": 9,
                "output_tokens": 4,
            },
        }

    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call_qwen_chat)
    provider = make_qwen_stage_provider(
        "introduction", qwen_call=fake_call_qwen_chat
    )
    output = provider({"stage": "introduction", "inputs": {}})

    assert len(calls) == 2
    assert output["draft"]["text"] == "introduction"
    assert output["_usage"]["repair_calls"] == 1
    assert output["_usage"]["repair_used"] is True
    assert output["_usage"]["repair"]["input_tokens"] == 9

    def always_broken(*args, **kwargs):
        return {
            "content": "not json",
            "_llm_usage": {"agent_name": "StagedAbstractAuthor"},
        }

    broken_provider = make_qwen_stage_provider(
        "abstract", qwen_call=always_broken
    )
    with pytest.raises(StagedCompletionError, match="after one repair call"):
        broken_provider({"stage": "abstract", "inputs": {}})


def test_multi_reviewer_aggregation_blocks_only_critical_consensus() -> None:
    def finding(finding_id: str, severity: str) -> ManuscriptReviewFinding:
        return ManuscriptReviewFinding(
            finding_id=finding_id,
            dimension="continuity",
            severity=severity,
            consensus="none",
            statement=f"statement {finding_id}",
        )

    reviewers = [
        ReviewerRole(
            reviewer_id="r1",
            role="continuity",
            findings=[
                finding("A", "critical"),
                finding("B", "major"),
                finding("C", "advisory"),
            ],
        ),
        ReviewerRole(
            reviewer_id="r2",
            role="clarity",
            findings=[finding("A", "critical"), finding("B", "major")],
        ),
        ReviewerRole(
            reviewer_id="r3",
            role="reader_flow",
            findings=[finding("A", "critical")],
        ),
    ]
    aggregated = aggregate_multi_reviewer_report(
        MultiReviewerReport(reviewers=reviewers)
    )
    by_id = {item.finding_id: item for item in aggregated.findings}
    assert by_id["A"].severity == "critical"
    assert by_id["A"].consensus == "consensus"
    assert by_id["A"].blocking is True
    assert by_id["B"].severity == "major"
    assert by_id["B"].consensus == "majority"
    assert by_id["B"].blocking is False
    assert by_id["C"].consensus == "split"
    assert by_id["C"].blocking is False
    assert aggregated.fail_open is False

    advisory_only = MultiReviewerReport(
        reviewers=[
            ReviewerRole(
                reviewer_id="r1",
                role="logic",
                findings=[finding("D", "advisory")],
            ),
            ReviewerRole(
                reviewer_id="r2",
                role="overlap",
                findings=[finding("D", "advisory")],
            ),
        ]
    )
    advisory_aggregated = aggregate_multi_reviewer_report(advisory_only)
    assert advisory_aggregated.findings[0].consensus == "consensus"
    assert advisory_aggregated.findings[0].blocking is False
    assert advisory_aggregated.fail_open is True


def test_qwen_provider_does_not_pin_transport_key_candidates(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_call_qwen_chat(*args, **kwargs):
        captured.update(kwargs)
        return {
            "content": json.dumps({"status": "drafted"}),
            "_llm_usage": {},
        }

    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call_qwen_chat)
    provider = make_qwen_stage_provider(
        "conclusion", qwen_call=fake_call_qwen_chat
    )
    provider({"stage": "conclusion", "inputs": {}})
    assert "max_transport_key_candidates" not in captured


def test_multi_reviewer_qwen_provider_mocked_aggregation_and_usage(
    monkeypatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_call_qwen_chat(*args, **kwargs):
        calls.append({"agent": args[0], "messages": args[1]})
        user = json.loads(args[1][1]["content"])
        stage_input = user["stage_input"]
        findings = [
            {
                "finding_id": "critical-overlap",
                "dimension": "overlap",
                "severity": "critical",
                "consensus": "none",
                "statement": "same overlap issue",
                "advisory": False,
                "blocking": False,
            }
        ]
        return {
            "content": json.dumps(
                {
                    "reviewer_id": stage_input.get("reviewer_id"),
                    "role": stage_input.get("reviewer_role"),
                    "findings": findings,
                }
            ),
            "_llm_usage": {
                "agent_name": "StagedWholeManuscriptReviewer",
                "model_tier": "c_model",
                "model_name": "qwen3.5-plus",
                "input_tokens": 5,
                "output_tokens": 2,
                "mock_llm": False,
                "success": True,
            },
        }

    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call_qwen_chat)
    provider = make_multi_reviewer_qwen_provider(
        reviewers=[
            {"reviewer_id": "r1", "role": "overlap"},
            {"reviewer_id": "r2", "role": "clarity"},
        ],
        qwen_call=fake_call_qwen_chat,
    )
    output = provider({"stage": "whole_manuscript_review", "inputs": {}})

    assert len(calls) == 2
    assert all(call["agent"] == "StagedWholeManuscriptReviewer" for call in calls)
    review = output["review"]
    assert review["findings"][0]["severity"] == "critical"
    assert review["findings"][0]["consensus"] == "consensus"
    assert review["findings"][0]["blocking"] is True
    assert review["fail_open"] is False
    usage = output["_usage"]
    assert usage["call_count"] == 2
    assert usage["total_input_tokens"] == 10
    assert usage["total_output_tokens"] == 4
    assert set(usage["reviewers"].keys()) == {"r1", "r2"}
    assert usage["repair_calls"] == 0


def test_aggregate_assigns_deterministic_ids_and_preserves_existing() -> None:
    blank = ManuscriptReviewFinding(
        finding_id="",
        dimension="logic",
        severity="critical",
        consensus="none",
        statement="same logic issue",
    )
    keep = ManuscriptReviewFinding(
        finding_id="KEEP-1",
        dimension="clarity",
        severity="advisory",
        consensus="none",
        statement="clarity note",
    )
    report = MultiReviewerReport(
        reviewers=[
            ReviewerRole(reviewer_id="r1", role="logic", findings=[blank]),
            ReviewerRole(
                reviewer_id="r2", role="clarity", findings=[blank, keep]
            ),
        ]
    )
    first = aggregate_multi_reviewer_report(report)
    second = aggregate_multi_reviewer_report(report)
    assert [finding.finding_id for finding in first.findings] == [
        finding.finding_id for finding in second.findings
    ]
    by_id = {finding.finding_id: finding for finding in first.findings}
    assert by_id["GMR-001"].severity == "critical"
    assert by_id["GMR-001"].consensus == "consensus"
    assert by_id["GMR-001"].blocking is True
    assert by_id["KEEP-1"].finding_id == "KEEP-1"
    assert by_id["KEEP-1"].blocking is False


def test_schema_hub_reexports_provider_and_reviewer_types() -> None:
    from optomind_research.runtime.article_completion_schemas import (
        MultiReviewerReport as HubMultiReviewerReport,
        QwenMultiReviewerProvider as HubQwenMultiReviewerProvider,
        QwenStagedProvider as HubQwenStagedProvider,
        ReviewerRole as HubReviewerRole,
        aggregate_multi_reviewer_report as hub_aggregate,
        make_multi_reviewer_qwen_provider as hub_make_multi,
        make_qwen_stage_provider as hub_make_single,
    )

    assert HubReviewerRole is ReviewerRole
    assert HubMultiReviewerReport is MultiReviewerReport
    assert hub_aggregate is aggregate_multi_reviewer_report
    assert HubQwenStagedProvider is QwenStagedProvider
    assert HubQwenMultiReviewerProvider is QwenMultiReviewerProvider
    assert hub_make_single is make_qwen_stage_provider
    assert hub_make_multi is make_multi_reviewer_qwen_provider


def test_stage_specific_inputs_and_resume_fingerprint_change(
    tmp_path: Path,
) -> None:
    received: dict[str, dict[str, Any]] = {}

    def make_provider(name: str):
        def provider(stage_input: Mapping[str, Any]) -> dict[str, Any]:
            received[name] = dict(stage_input.get("inputs") or {})
            return {
                "workplan": {"derived_from": []},
                "draft": {"text": name},
                "status": "offline",
            }

        return provider

    providers = {
        "conclusion": make_provider("conclusion"),
        "introduction": make_provider("introduction"),
        "abstract": make_provider("abstract"),
    }
    stage_inputs = {
        "conclusion": {"kind": "conclusion-selected"},
        "introduction": {"kind": "introduction-selected"},
    }
    first = run_staged_article_completion(
        work_dir=tmp_path / "out",
        inputs={"kind": "global"},
        stage_inputs=stage_inputs,
        stage_providers=providers,
        run_id="stage-inputs",
    )
    assert received["conclusion"] == {"kind": "conclusion-selected"}
    assert received["introduction"] == {"kind": "introduction-selected"}
    assert received["abstract"] == {"kind": "global"}

    received.clear()
    second = run_staged_article_completion(
        work_dir=tmp_path / "out",
        inputs={"kind": "global"},
        stage_inputs=stage_inputs,
        stage_providers=providers,
        resume=True,
        run_id="stage-inputs",
    )
    assert received == {}
    assert second.stages["conclusion"].status == "noop"

    with pytest.raises(StagedCompletionError, match="fingerprint changed"):
        run_staged_article_completion(
            work_dir=tmp_path / "out",
            inputs={"kind": "global"},
            stage_inputs={"conclusion": {"kind": "conclusion-changed"}},
            stage_providers=providers,
            resume=True,
            run_id="stage-inputs",
        )


def test_commander_editor_provider_semantic_approval(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []

    def fake_call_qwen_chat(*args, **kwargs):
        calls.append(args[0])
        return {
            "content": json.dumps(
                {
                    "proposals": [
                        {
                            "patch_id": "",
                            "operation": "move_block",
                            "target": "S02",
                            "rationale": "ordering",
                        },
                        {
                            "patch_id": "",
                            "operation": "merge_blocks",
                            "target": "S03",
                            "rationale": "duplication",
                            "claim_text_change": True,
                        },
                    ]
                }
            ),
            "_llm_usage": {},
        }

    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call_qwen_chat)
    provider = make_qwen_stage_provider(
        "bounded_patch_proposals", qwen_call=fake_call_qwen_chat
    )
    output = provider({"stage": "bounded_patch_proposals", "inputs": {}})

    assert calls == ["StagedCommanderEditor"]
    assert output["proposals"][0]["operation"] == "move_block"
    assert output["proposals"][1]["operation"] == "merge_blocks"
    normalized = [
        normalize_patch_proposal(proposal) for proposal in output["proposals"]
    ]
    assert normalized[0].approval_required is False
    assert normalized[1].approval_required is True

    state = run_staged_article_completion(
        work_dir=tmp_path / "out",
        stage_providers={"bounded_patch_proposals": provider},
        run_id="commander-editor",
    )
    stage = state.stages["bounded_patch_proposals"]
    assert stage.status == "awaiting_approval"
    assert stage.approval_required is True


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
                usage={"input_tokens": 5, "output_tokens": 2},
            )
        },
    )


def _make_editorial_qwen_mock(
    *,
    verifier_ok: bool = True,
    revised_map: Mapping[str, str] | None = None,
):
    calls: list[dict[str, Any]] = []

    def fake_call_qwen_chat(agent, messages, **kwargs):
        calls.append({"agent": agent, "messages": messages})
        user = json.loads(messages[1]["content"])["stage_input"]
        if agent == "StagedEditorialRevisionAuthor":
            original = str(user.get("original_text") or "")
            revised = ""
            for marker, replacement in (revised_map or {}).items():
                if marker in original:
                    revised = replacement
                    break
            return {
                "content": json.dumps(
                    {"revised_text": revised, "notes": ""}
                ),
                "_llm_usage": {
                    "agent_name": agent,
                    "model_tier": "c_model",
                    "input_tokens": 3,
                    "output_tokens": 1,
                },
            }
        return {
            "content": json.dumps(
                {
                    "meaning_preserved": verifier_ok,
                    "scope_preserved": verifier_ok,
                    "citations_preserved": verifier_ok,
                    "numbers_conditions_preserved": verifier_ok,
                    "problem_improved": verifier_ok,
                    "notes": "ok" if verifier_ok else "problem not improved",
                }
            ),
            "_llm_usage": {
                "agent_name": agent,
                "model_tier": "c2_model",
                "input_tokens": 2,
                "output_tokens": 1,
            },
        }

    return fake_call_qwen_chat, calls


def _editorial_stage_input(
    *,
    sections: list[dict[str, Any]] | None = None,
    findings: list[dict[str, Any]] | None = None,
    proposals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    blocks = sections or [
        {"block_id": "S01-B001", "prose": "Old transition here [REF:p1]."},
        {"block_id": "S01-B002", "prose": "Middle block content."},
        {
            "block_id": "S01-B003",
            "prose": "Jargon term needs clarification [REF:p3].",
        },
    ]
    return {
        "stage": "editorial_revision",
        "inputs": {
            "sections": [
                {
                    "section_id": "S01",
                    "section_title": "First Section",
                    "chapter_thesis": "thesis",
                    "reader_takeaway": "takeaway",
                    "blocks": blocks,
                }
            ]
        },
        "previous_artifacts": {
            "whole_manuscript_review": {
                "fingerprint": "review-fp",
                "payload": {
                    "review": {
                        "findings": findings or [],
                        "fail_open": True,
                    },
                    "status": "reviewed",
                },
            },
            "bounded_patch_proposals": {
                "fingerprint": "patches-fp",
                "payload": {
                    "proposals": proposals or [],
                    "approval_required": False,
                },
            },
            "conclusion": {
                "fingerprint": "c-fp",
                "payload": {
                    "draft": {"text": "conclusion text"},
                    "status": "drafted",
                },
            },
            "introduction": {
                "fingerprint": "i-fp",
                "payload": {
                    "draft": {"text": "introduction text"},
                    "status": "drafted",
                },
            },
            "abstract": {
                "fingerprint": "a-fp",
                "payload": {
                    "draft": {"text": "abstract text"},
                    "status": "drafted",
                },
            },
        },
    }


def _editorial_finding(
    *,
    finding_id: str,
    issue_type: str,
    target_ids: list[str],
    severity: str = "advisory",
    consensus: str = "none",
    dimension: str = "continuity",
    statement: str = "editorial note",
) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "issue_key": f"{issue_type}:{','.join(target_ids)}",
        "issue_type": issue_type,
        "target_ids": target_ids,
        "dimension": dimension,
        "severity": severity,
        "consensus": consensus,
        "statement": statement,
    }


def test_staged_cli_parser_defaults(tmp_path: Path) -> None:
    parser = cli.build_arg_parser()
    args = parser.parse_args(
        [
            "--inputs-json",
            str(tmp_path / "inputs.json"),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    assert args.run_id == ""
    assert args.resume is False
    assert args.model_tier == "c_model"
    assert args.model_tier_explicit is False
    assert args.stage_model_tiers is None
    assert args.editorial_verifier_tier == "c2_model"
    assert args.live_stages == ""
    assert args.stage_inputs_json is None
    assert args.metadata_json is None
    assert args.reviewer_roles == ",".join(cli.DEFAULT_REVIEWER_ROLES)


# --------------------------------------------------------------------------- #
# Presentation IR and title planner tests
# --------------------------------------------------------------------------- #


def test_presentation_ir_model_defaults() -> None:
    ir = LiteratureReviewPresentationIR()
    assert ir.schema_version == PRESENTATION_IR_SCHEMA
    assert ir.central_topic == ""
    assert ir.synthesis_claims == []
    assert ir.forbidden_claims == []
    assert ir.terminology_registry == {}


def test_presentation_ir_extraction_fail_open_on_missing() -> None:
    assert extract_presentation_ir({}) is None
    assert extract_presentation_ir({"presentation_ir": None}) is None
    assert extract_presentation_ir({"presentation_ir": "not-a-dict"}) is None


def test_presentation_ir_extraction_fail_open_on_invalid() -> None:
    # Field types are lenient (all optional), so validate can't really fail,
    # but a completely wrong type at root level should return None.
    assert extract_presentation_ir({"presentation_ir": []}) is None


def test_presentation_ir_extraction_from_flat_inputs() -> None:
    raw = {
        "presentation_ir": {
            "central_topic": "metamaterial absorbers",
            "review_subtype": "systematic",
            "synthesis_claims": ["claim A", "claim B"],
            "consensus": ["consensus point"],
            "controversies": ["open question"],
            "emerging_directions": ["direction X"],
            "safe_citations": ["s2:abc"],
            "forbidden_claims": ["overclaim Y"],
            "terminology_registry": {"TMA": "thin-film metamaterial absorber"},
        }
    }
    ir = extract_presentation_ir(raw)
    assert ir is not None
    assert ir.central_topic == "metamaterial absorbers"
    assert ir.review_subtype == "systematic"
    assert ir.synthesis_claims == ["claim A", "claim B"]
    assert ir.terminology_registry == {"TMA": "thin-film metamaterial absorber"}
    assert ir.ir_fingerprint != ""
    assert ir.provenance == "extracted_from_inputs"


def test_presentation_ir_extraction_from_handoff() -> None:
    raw = {
        "full_manuscript_handoff": {
            "presentation_ir": {
                "central_topic": "photonic sensors",
                "review_subtype": "narrative",
            }
        }
    }
    ir = extract_presentation_ir(raw)
    assert ir is not None
    assert ir.central_topic == "photonic sensors"


def test_presentation_ir_fingerprint_is_deterministic() -> None:
    raw = {
        "presentation_ir": {
            "central_topic": "waveguides",
            "review_subtype": "scoping",
            "synthesis_claims": ["c1", "c2"],
        }
    }
    ir1 = extract_presentation_ir(raw)
    ir2 = extract_presentation_ir(raw)
    assert ir1 is not None and ir2 is not None
    assert ir1.ir_fingerprint == ir2.ir_fingerprint


def test_plan_review_titles_deterministic_fallback_no_ir() -> None:
    plan = plan_review_titles(None)
    assert plan.fallback_used is True
    assert plan.provenance == "deterministic_fallback"
    assert 3 <= len(plan.candidates) <= 5
    assert plan.selected_title == plan.candidates[0].title
    assert plan.schema_version == TITLE_PLAN_SCHEMA


def test_plan_review_titles_deterministic_fallback_with_ir() -> None:
    ir = LiteratureReviewPresentationIR(
        central_topic="silicon photonics",
        review_subtype="systematic review",
    )
    plan = plan_review_titles(ir)
    assert plan.fallback_used is True
    assert "silicon photonics" in plan.selected_title.lower()
    assert all(isinstance(c, TitleCandidate) for c in plan.candidates)


def test_plan_review_titles_from_provider_enough_candidates() -> None:
    provider_candidates = [
        {"rank": 1, "title": "Deep Learning for Optics: A Survey", "rationale": "broad"},
        {"rank": 2, "title": "Optics and Deep Learning: Methods Review", "rationale": "methods"},
        {"rank": 3, "title": "Neural Approaches in Modern Optics", "rationale": "neural framing"},
    ]
    plan = plan_review_titles(None, candidates_from_provider=provider_candidates)
    assert plan.fallback_used is False
    assert plan.provenance == "provider"
    assert len(plan.candidates) == 3
    assert plan.selected_title == "Deep Learning for Optics: A Survey"


def test_plan_review_titles_from_provider_too_few_falls_back() -> None:
    provider_candidates = [
        {"rank": 1, "title": "Only One Title"},
    ]
    plan = plan_review_titles(None, candidates_from_provider=provider_candidates)
    assert plan.fallback_used is True


def test_plan_review_titles_from_provider_skips_blank_titles() -> None:
    provider_candidates = [
        {"rank": 1, "title": "   "},
        {"rank": 2, "title": ""},
        {"rank": 3, "title": "Real Title A"},
        {"rank": 4, "title": "Real Title B"},
        {"rank": 5, "title": "Real Title C"},
    ]
    plan = plan_review_titles(None, candidates_from_provider=provider_candidates)
    assert plan.fallback_used is False
    assert plan.selected_title == "Real Title A"


def test_plan_review_titles_extracts_subject_from_long_user_question() -> None:
    ir = LiteratureReviewPresentationIR(
        central_topic=(
            "The user seeks a comprehensive scholarly review of Bound States "
            "in the Continuum (BICs) and Quasi-BICs in photonics. The research "
            "question requires defining the physical mechanisms."
        ),
        review_subtype="critical literature review",
    )
    plan = plan_review_titles(ir)
    # The fallback extracts the subject from the user-question prefix and may
    # truncate at clause boundaries to avoid overly long titles.  Assert the
    # important invariants: topic starts with "Bound States", title is under
    # 150 chars, and the raw user instruction is not present verbatim.
    assert "Bound States" in plan.selected_title
    assert "The user" not in plan.selected_title
    assert len(plan.selected_title) < 150


def test_conclusion_workplan_carries_ir_fingerprint() -> None:
    wplan = ConclusionWorkplan(
        derived_from=["commander_structure"],
        outline=["answer", "limitations"],
        presentation_ir_fingerprint="fp123",
    )
    dumped = wplan.model_dump(mode="json")
    assert dumped["presentation_ir_fingerprint"] == "fp123"


def test_introduction_workplan_carries_ir_fingerprint() -> None:
    wplan = IntroductionWorkplan(
        promises_derived_from=["conclusion"],
        presentation_ir_fingerprint="fp456",
    )
    assert wplan.presentation_ir_fingerprint == "fp456"


def test_abstract_workplan_carries_ir_fingerprint() -> None:
    wplan = AbstractWorkplan(
        compresses=["conclusion", "introduction"],
        presentation_ir_fingerprint="fp789",
    )
    assert wplan.presentation_ir_fingerprint == "fp789"


def test_offline_provider_injects_ir_fingerprint_when_ir_present(tmp_path: Path) -> None:
    inputs = {
        "section_order": [],
        "presentation_ir": {
            "central_topic": "photoacoustic imaging",
            "review_subtype": "systematic",
            "synthesis_claims": ["SA"],
        },
    }
    state = run_staged_article_completion(
        work_dir=tmp_path,
        inputs=inputs,
        stage_order=["conclusion", "introduction", "abstract"],
    )
    assert state.all_completed
    for stage in ("conclusion", "introduction", "abstract"):
        record = state.stages[stage]
        wplan = record.payload.get("workplan") or {}
        fp = wplan.get("presentation_ir_fingerprint", "")
        assert fp != "", f"{stage} workplan has empty ir fingerprint"


def test_offline_provider_ir_missing_old_fallback_preserved(tmp_path: Path) -> None:
    inputs: dict[str, Any] = {"section_order": []}
    state = run_staged_article_completion(
        work_dir=tmp_path,
        inputs=inputs,
        stage_order=["conclusion", "introduction", "abstract"],
    )
    assert state.all_completed
    for stage in ("conclusion", "introduction", "abstract"):
        record = state.stages[stage]
        wplan = record.payload.get("workplan") or {}
        # IR fingerprint empty = old fallback path
        assert wplan.get("presentation_ir_fingerprint", "") == ""


def test_front_matter_stages_constant() -> None:
    assert FRONT_MATTER_STAGES == {"conclusion", "introduction", "abstract"}


def test_stage_input_carries_ir_for_front_matter(tmp_path: Path) -> None:
    """IR is present in stage_input for front-matter stages when in inputs."""
    received_ir_by_stage: dict[str, Any] = {}

    def _capture_provider(stage_input: Any) -> Any:
        stage = str(stage_input.get("stage") or "")
        received_ir_by_stage[stage] = stage_input.get("presentation_ir")
        from optomind_research.runtime.staged_article_completion import (
            _default_offline_stage_provider,
        )
        return _default_offline_stage_provider(stage_input)

    inputs = {
        "section_order": [],
        "presentation_ir": {
            "central_topic": "THz sensors",
            "review_subtype": "scoping",
            "synthesis_claims": [],
        },
    }
    run_staged_article_completion(
        work_dir=tmp_path,
        inputs=inputs,
        stage_order=["conclusion", "introduction", "abstract"],
        stage_providers={
            "conclusion": _capture_provider,
            "introduction": _capture_provider,
            "abstract": _capture_provider,
        },
    )
    for stage in ("conclusion", "introduction", "abstract"):
        ir_data = received_ir_by_stage.get(stage)
        assert isinstance(ir_data, dict), f"{stage} did not receive presentation_ir"
        assert ir_data.get("central_topic") == "THz sensors"
def test_staged_cli_offline_run_prints_compact_summary(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(json.dumps({"topic": "x"}), encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return _cli_fake_state()

    monkeypatch.setattr(cli, "run_staged_article_completion", fake_run)
    code = cli.main(
        [
            "--inputs-json",
            str(inputs_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--run-id",
            "cli-run",
        ]
    )
    assert code == 0
    assert captured["run_id"] == "cli-run"
    assert captured["stage_providers"] == {}  # no live stages -> no Qwen
    out = capsys.readouterr().out
    summary = json.loads(out)
    assert summary["status"] == "completed"
    assert summary["stage_statuses"]["conclusion"] == "completed"
    assert summary["approval_required_stages"] == []
    assert summary["total_input_tokens"] == 5
    assert summary["total_output_tokens"] == 2


def test_staged_cli_live_stages_build_only_allowlisted_providers(
    tmp_path: Path, monkeypatch
) -> None:
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(json.dumps({"topic": "x"}), encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return _cli_fake_state()

    monkeypatch.setattr(cli, "run_staged_article_completion", fake_run)
    code = cli.main(
        [
            "--inputs-json",
            str(inputs_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--live-stages",
            "conclusion,whole_manuscript_review",
        ]
    )
    assert code == 0
    providers = captured["stage_providers"]
    assert set(providers.keys()) == {
        "conclusion",
        "whole_manuscript_review",
    }
    assert isinstance(providers["conclusion"], QwenStagedProvider)
    assert isinstance(
        providers["whole_manuscript_review"], QwenMultiReviewerProvider
    )
    assert [
        spec["reviewer_id"] for spec in providers[
            "whole_manuscript_review"
        ].reviewers
    ] == list(cli.DEFAULT_REVIEWER_ROLES)


def test_staged_cli_stage_inputs_and_metadata_loaded(
    tmp_path: Path, monkeypatch
) -> None:
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(json.dumps({"topic": "x"}), encoding="utf-8")
    stage_inputs_path = tmp_path / "stage_inputs.json"
    stage_inputs_path.write_text(
        json.dumps({"conclusion": {"kind": "selected"}}), encoding="utf-8"
    )
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(json.dumps({"topic": "meta"}), encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return _cli_fake_state()

    monkeypatch.setattr(cli, "run_staged_article_completion", fake_run)
    code = cli.main(
        [
            "--inputs-json",
            str(inputs_path),
            "--stage-inputs-json",
            str(stage_inputs_path),
            "--metadata-json",
            str(metadata_path),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    assert code == 0
    assert captured["inputs"] == {"topic": "x"}
    assert captured["stage_inputs"] == {"conclusion": {"kind": "selected"}}
    assert captured["metadata"] == {"topic": "meta"}


def test_staged_cli_errors_for_unknown_stage_and_missing_files(
    tmp_path: Path, capsys
) -> None:
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(json.dumps({"topic": "x"}), encoding="utf-8")
    output = tmp_path / "out"

    code = cli.main(
        [
            "--inputs-json",
            str(inputs_path),
            "--output-dir",
            str(output),
            "--live-stages",
            "bogus",
        ]
    )
    assert code == 2
    assert "unknown live stage" in capsys.readouterr().err

    code_missing = cli.main(
        [
            "--inputs-json",
            str(tmp_path / "missing.json"),
            "--output-dir",
            str(output),
        ]
    )
    assert code_missing == 2
    assert "missing inputs JSON" in capsys.readouterr().err

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{not json", encoding="utf-8")
    code_invalid = cli.main(
        [
            "--inputs-json",
            str(invalid),
            "--output-dir",
            str(output),
        ]
    )
    assert code_invalid == 2
    assert "cannot read inputs JSON" in capsys.readouterr().err

    code_stage_missing = cli.main(
        [
            "--inputs-json",
            str(inputs_path),
            "--stage-inputs-json",
            str(tmp_path / "missing_stage.json"),
            "--output-dir",
            str(output),
        ]
    )
    assert code_stage_missing == 2
    assert "missing stage inputs JSON" in capsys.readouterr().err


def test_staged_cli_summary_counts_nested_usage_without_double_counting(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(json.dumps({"topic": "x"}), encoding="utf-8")
    state = StagedArticleCompletionState(
        run_id="cli",
        run_fingerprint="fp",
        stage_order=list(STAGE_ORDER),
        status="completed",
        stages={
            "conclusion": StagedStageState(
                stage="conclusion",
                status="completed",
                usage={
                    "initial": {"input_tokens": 3, "output_tokens": 1},
                    "repair": {"input_tokens": 9, "output_tokens": 4},
                    "repair_calls": 1,
                    "repair_used": True,
                },
            ),
            "whole_manuscript_review": StagedStageState(
                stage="whole_manuscript_review",
                status="completed",
                usage={
                    "total_input_tokens": 10,
                    "total_output_tokens": 4,
                    "reviewers": {
                        "r1": {"input_tokens": 5, "output_tokens": 2},
                        "r2": {"input_tokens": 5, "output_tokens": 2},
                    },
                },
            ),
        },
    )

    def fake_run(**kwargs):
        return state

    monkeypatch.setattr(cli, "run_staged_article_completion", fake_run)
    code = cli.main(
        [
            "--inputs-json",
            str(inputs_path),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["total_input_tokens"] == 22  # 3 + 9 + aggregate 10
    assert summary["total_output_tokens"] == 9  # 1 + 4 + aggregate 4


def test_staged_cli_injects_handoff_and_commander_work_order(
    tmp_path: Path, capsys
) -> None:
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(
        json.dumps(
            {
                "topic": "x",
                "full_manuscript_handoff": {
                    "input_fingerprint": "old-fp"
                },
            }
        ),
        encoding="utf-8",
    )
    handoff_path = tmp_path / "handoff.json"
    handoff_path.write_text(
        json.dumps(
            {
                "schema_version": "optomind.full_manuscript_handoff.v1",
                "input_fingerprint": "handoff-fp",
                "section_order": ["S01"],
                "sections": {},
                "aggregate_counts": {},
                "repair_notes": [],
                "hard_defects": [],
            }
        ),
        encoding="utf-8",
    )
    work_order_path = tmp_path / "work_order.json"
    work_order_path.write_text(
        json.dumps(
            {
                "schema_version": (
                    "optomind.global_manuscript_commander.work_order.v2"
                ),
                "status": "completed",
                "fingerprint": "wo-fp",
                "proposed_section_order": [
                    {"section_id": "S01", "position": 0, "rationale": "r"}
                ],
                "section_decisions": [],
                "read_only_declaration": {
                    "chapter_text_changed": False,
                    "retrieval_launched": False,
                },
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "out"
    code = cli.main(
        [
            "--inputs-json",
            str(inputs_path),
            "--output-dir",
            str(output),
            "--handoff-json",
            str(handoff_path),
            "--commander-work-order-json",
            str(work_order_path),
        ]
    )
    assert code == 0

    handoff_artifact = json.loads(
        (output / "staged_handoff_metadata_repair.json").read_text(
            encoding="utf-8"
        )
    )
    assert "full_manuscript_handoff:handoff-fp" in handoff_artifact["payload"][
        "metadata_repair_notes"
    ]
    commander_artifact = json.loads(
        (output / "staged_commander_structure.json").read_text(
            encoding="utf-8"
        )
    )
    assert (
        commander_artifact["payload"]["section_order"][0]["section_id"]
        == "S01"
    )
    assert commander_artifact["payload"]["source_work_order_path"] == str(
        work_order_path
    )


def test_staged_cli_handoff_and_work_order_file_errors(
    tmp_path: Path, capsys
) -> None:
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(json.dumps({"topic": "x"}), encoding="utf-8")
    output = tmp_path / "out"

    code = cli.main(
        [
            "--inputs-json",
            str(inputs_path),
            "--output-dir",
            str(output),
            "--handoff-json",
            str(tmp_path / "missing_handoff.json"),
        ]
    )
    assert code == 2
    assert "missing handoff JSON" in capsys.readouterr().err

    invalid = tmp_path / "invalid_handoff.json"
    invalid.write_text("{not json", encoding="utf-8")
    code_invalid = cli.main(
        [
            "--inputs-json",
            str(inputs_path),
            "--output-dir",
            str(output),
            "--handoff-json",
            str(invalid),
        ]
    )
    assert code_invalid == 2
    assert "cannot read handoff JSON" in capsys.readouterr().err

    code_wo = cli.main(
        [
            "--inputs-json",
            str(inputs_path),
            "--output-dir",
            str(output),
            "--commander-work-order-json",
            str(tmp_path / "missing_work_order.json"),
        ]
    )
    assert code_wo == 2
    assert "missing commander work order JSON" in capsys.readouterr().err


def test_cli_default_stage_model_tiers_route_composers_and_reviewers(
    tmp_path: Path, monkeypatch
) -> None:
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(json.dumps({"topic": "x"}), encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return _cli_fake_state()

    monkeypatch.setattr(cli, "run_staged_article_completion", fake_run)
    code = cli.main(
        [
            "--inputs-json",
            str(inputs_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--live-stages",
            ",".join(sorted(cli.LIVE_PROVIDER_STAGES)),
        ]
    )
    assert code == 0
    providers = captured["stage_providers"]
    for stage in (
        "conclusion",
        "introduction",
        "abstract",
        "bounded_patch_proposals",
    ):
        assert providers[stage].model_tier == "c_model", stage
    assert providers["whole_manuscript_review"].model_tier == "c2_model"
    assert providers["editorial_revision"].model_tier == "c_model"
    assert providers["editorial_revision"].verifier_tier == "c2_model"


def test_cli_explicit_model_tier_preserves_old_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(json.dumps({"topic": "x"}), encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return _cli_fake_state()

    monkeypatch.setattr(cli, "run_staged_article_completion", fake_run)
    code = cli.main(
        [
            "--inputs-json",
            str(inputs_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--model-tier",
            "c2_model",
            "--live-stages",
            "conclusion,whole_manuscript_review",
        ]
    )
    assert code == 0
    providers = captured["stage_providers"]
    assert providers["conclusion"].model_tier == "c2_model"
    assert providers["whole_manuscript_review"].model_tier == "c2_model"


def test_cli_stage_model_tier_mapping_overrides_defaults(
    tmp_path: Path, monkeypatch
) -> None:
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(json.dumps({"topic": "x"}), encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return _cli_fake_state()

    monkeypatch.setattr(cli, "run_staged_article_completion", fake_run)
    code = cli.main(
        [
            "--inputs-json",
            str(inputs_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--live-stages",
            "conclusion,introduction,abstract,whole_manuscript_review,"
            "editorial_revision",
            "--stage-model-tier",
            "conclusion=c2_model",
            "--stage-model-tier",
            "whole_manuscript_review=c_model",
            "--stage-model-tier",
            "editorial_revision=b_model",
        ]
    )
    assert code == 0
    providers = captured["stage_providers"]
    assert providers["conclusion"].model_tier == "c2_model"
    assert providers["introduction"].model_tier == "c_model"
    assert providers["abstract"].model_tier == "c_model"
    assert providers["whole_manuscript_review"].model_tier == "c_model"
    assert providers["editorial_revision"].model_tier == "b_model"
    assert providers["editorial_revision"].verifier_tier == "c2_model"

    captured.clear()
    code = cli.main(
        [
            "--inputs-json",
            str(inputs_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--model-tier",
            "b_model",
            "--live-stages",
            "conclusion,abstract",
            "--stage-model-tier",
            "abstract=c_model",
        ]
    )
    assert code == 0
    providers = captured["stage_providers"]
    assert providers["conclusion"].model_tier == "b_model"
    assert providers["abstract"].model_tier == "c_model"


def test_cli_stage_model_tier_invalid_entries(
    tmp_path: Path, capsys
) -> None:
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(json.dumps({"topic": "x"}), encoding="utf-8")
    output = tmp_path / "out"
    base = [
        "--inputs-json",
        str(inputs_path),
        "--output-dir",
        str(output),
    ]

    code = cli.main(base + ["--stage-model-tier", "bogus=c_model"])
    assert code == 2
    assert "unknown or unsupported stage" in capsys.readouterr().err

    code = cli.main(base + ["--stage-model-tier", "conclusion=not_a_tier"])
    assert code == 2
    assert "unsupported model tier for conclusion" in capsys.readouterr().err

    code = cli.main(
        base
        + [
            "--stage-model-tier",
            "conclusion=c_model",
            "--stage-model-tier",
            "conclusion=c2_model",
        ]
    )
    assert code == 2
    assert "duplicate --stage-model-tier for stage" in capsys.readouterr().err

    code = cli.main(base + ["--stage-model-tier", "conclusionc_model"])
    assert code == 2
    assert "expected STAGE=TIER" in capsys.readouterr().err

    assert cli._parse_stage_model_tiers(None) == {}
    assert cli._parse_stage_model_tiers([]) == {}


def test_multi_reviewer_provider_independent_inputs(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_call_qwen_chat(*args, **kwargs):
        user = json.loads(args[1][1]["content"])
        calls.append(dict(user["stage_input"]))
        return {
            "content": json.dumps(
                {
                    "findings": [
                        {
                            "dimension": "clarity",
                            "severity": "advisory",
                            "statement": "note",
                        }
                    ]
                }
            ),
            "_llm_usage": {},
        }

    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call_qwen_chat)
    provider = make_multi_reviewer_qwen_provider(
        reviewers=[
            {"reviewer_id": "r1", "role": "continuity"},
            {"reviewer_id": "r2", "role": "clarity"},
        ],
        qwen_call=fake_call_qwen_chat,
    )
    provider(
        {
            "stage": "whole_manuscript_review",
            "inputs": {},
            "previous_artifacts": {
                "whole_manuscript_review": {"fingerprint": "stripped"},
                "reviewer_outputs": {"r1": {"findings": []}},
                "conclusion": {"fingerprint": "kept"},
            },
        }
    )
    assert len(calls) == 2
    for call_input in calls:
        assert call_input["reviewer_id"] in {"r1", "r2"}
        assert "reviewer_outputs" not in call_input
        assert "other_reviewer_outputs" not in call_input
        previous = call_input.get("previous_artifacts") or {}
        assert "whole_manuscript_review" not in previous
        assert "reviewer_outputs" not in previous
        assert "conclusion" in previous
    assert [call_input["reviewer_id"] for call_input in calls] == [
        "r1",
        "r2",
    ]


def test_model_provided_consensus_blocking_and_ids_overridden(
    monkeypatch,
) -> None:
    def fake_call_qwen_chat(*args, **kwargs):
        return {
            "content": json.dumps(
                {
                    "findings": [
                        {
                            "finding_id": "model-id",
                            "dimension": "logic",
                            "severity": "major",
                            "consensus": "consensus",
                            "statement": "same logic gap",
                            "advisory": False,
                            "blocking": True,
                        },
                        {
                            "finding_id": "evidence-id",
                            "dimension": "evidence_sufficiency",
                            "severity": "critical",
                            "consensus": "consensus",
                            "statement": "out of scope",
                            "blocking": True,
                        },
                    ]
                }
            ),
            "_llm_usage": {},
        }

    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call_qwen_chat)
    provider = make_multi_reviewer_qwen_provider(
        reviewers=[
            {"reviewer_id": "r1", "role": "logic"},
            {"reviewer_id": "r2", "role": "logic"},
        ],
        qwen_call=fake_call_qwen_chat,
    )
    output = provider({"stage": "whole_manuscript_review", "inputs": {}})
    review = output["review"]
    assert review["fail_open"] is True
    assert len(review["findings"]) == 1
    finding = review["findings"][0]
    assert finding["severity"] == "major"
    assert finding["consensus"] == "consensus"
    assert finding["blocking"] is False
    assert finding["advisory"] is True
    assert finding["finding_id"] == "GMR-001"
    assert "evidence_sufficiency" not in json.dumps(review)
    for role in output["reviewers"]:
        stored = role["findings"][0]
        assert stored["finding_id"] == ""
        assert stored["consensus"] == "none"
        assert stored["blocking"] is False
        assert stored["advisory"] is True


def test_review_grouping_by_stable_issue_identity() -> None:
    def finding(**kwargs):
        defaults = dict(
            finding_id="",
            dimension="clarity",
            severity="advisory",
            consensus="none",
            statement="",
        )
        defaults.update(kwargs)
        return ManuscriptReviewFinding(**defaults)

    keyed = MultiReviewerReport(
        reviewers=[
            ReviewerRole(
                reviewer_id="r1",
                role="clarity",
                findings=[finding(issue_key="flow-gap", statement="prose one")],
            ),
            ReviewerRole(
                reviewer_id="r2",
                role="clarity",
                findings=[
                    finding(issue_key="flow-gap", statement="prose two")
                ],
            ),
        ]
    )
    aggregated = aggregate_multi_reviewer_report(keyed)
    assert len(aggregated.findings) == 1
    assert aggregated.findings[0].consensus == "consensus"
    assert aggregated.findings[0].blocking is False

    typed = MultiReviewerReport(
        reviewers=[
            ReviewerRole(
                reviewer_id="r1",
                role="overlap",
                findings=[
                    finding(
                        issue_type="role_overlap",
                        target_ids=["S02", "S01"],
                        statement="one wording",
                    )
                ],
            ),
            ReviewerRole(
                reviewer_id="r2",
                role="overlap",
                findings=[
                    finding(
                        issue_type="role_overlap",
                        target_ids=["S01", "S02"],
                        statement="another wording",
                    )
                ],
            ),
        ]
    )
    aggregated_typed = aggregate_multi_reviewer_report(typed)
    assert len(aggregated_typed.findings) == 1

    distinct = MultiReviewerReport(
        reviewers=[
            ReviewerRole(
                reviewer_id="r1",
                role="logic",
                findings=[finding(issue_key="a", statement="same prose")],
            ),
            ReviewerRole(
                reviewer_id="r2",
                role="logic",
                findings=[finding(issue_key="b", statement="same prose")],
            ),
        ]
    )
    assert len(aggregate_multi_reviewer_report(distinct).findings) == 2

    critical = MultiReviewerReport(
        reviewers=[
            ReviewerRole(
                reviewer_id="r1",
                role="logic",
                findings=[
                    finding(
                        issue_key="fatal",
                        severity="critical",
                        statement="wording one",
                    )
                ],
            ),
            ReviewerRole(
                reviewer_id="r2",
                role="logic",
                findings=[
                    finding(
                        issue_key="fatal",
                        severity="critical",
                        statement="wording two",
                    )
                ],
            ),
        ]
    )
    blocked = aggregate_multi_reviewer_report(critical)
    assert len(blocked.findings) == 1
    assert blocked.findings[0].blocking is True
    assert blocked.fail_open is False


def test_prompt_contracts_stage_specific_constraints() -> None:
    prompts_dir = Path(__file__).resolve().parents[1] / "prompts"
    conclusion = (
        prompts_dir / "Staged Conclusion Author.txt"
    ).read_text(encoding="utf-8")
    introduction = (
        prompts_dir / "Staged Introduction Author.txt"
    ).read_text(encoding="utf-8")
    abstract = (
        prompts_dir / "Staged Abstract Author.txt"
    ).read_text(encoding="utf-8")
    reviewer = (
        prompts_dir / "Staged Whole Manuscript Reviewer.txt"
    ).read_text(encoding="utf-8")
    editor = (
        prompts_dir / "Staged Commander Editor.txt"
    ).read_text(encoding="utf-8")

    assert "500-900" in conclusion
    assert "commander structural context only" in conclusion
    assert "do not draw on the introduction" in conclusion
    assert "limitations" in conclusion
    assert "outlook" in conclusion
    assert "new scientific claims" in conclusion

    assert "800-1300" in introduction
    assert "field background" in introduction
    assert "significance" in introduction
    assert "problem tension" in introduction
    assert "review scope/contribution" in introduction
    assert "roadmap" in introduction
    assert "local background candidates first" in introduction
    assert "retrieval_proposals" in introduction
    assert "never launches retrieval" in introduction

    assert "220-300" in abstract
    assert "written last" in abstract
    assert "citations" in abstract

    assert "no other reviewer" in reviewer
    for dim in ("continuity", "clarity", "reader_flow", "logic", "overlap"):
        assert dim in reviewer
    for issue_type in (
        "missing_transition",
        "duplicated_explanation",
        "undefined_term",
        "ordering_break",
        "logic_conflict",
    ):
        assert issue_type in reviewer
    assert "issue_key is only a fallback" in reviewer
    assert "re-adjudicate claim/evidence binding" in reviewer
    assert "issue_key" in reviewer
    assert "issue_type" in reviewer
    assert "target_ids" in reviewer

    assert "never output an entire manuscript" in editor
    assert "entire manuscript" in editor
    assert "citation payloads" in editor
    assert "PatchProposalSet" in editor
    assert "approval_required=true" in editor
    assert "rewrite_transition" in editor
    assert "wording-only transition edit" in editor
    assert "never auto-materialized by editorial_revision" in editor


def test_cli_summary_records_actual_stage_model_tiers(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(json.dumps({"topic": "x"}), encoding="utf-8")
    state = StagedArticleCompletionState(
        run_id="cli",
        run_fingerprint="fp",
        stage_order=list(STAGE_ORDER),
        status="completed",
        stages={
            "conclusion": StagedStageState(
                stage="conclusion",
                status="completed",
                usage={
                    "model_tier": "c_model",
                    "input_tokens": 5,
                    "output_tokens": 2,
                },
            ),
            "whole_manuscript_review": StagedStageState(
                stage="whole_manuscript_review",
                status="completed",
                usage={
                    "model_tier": "c2_model",
                    "total_input_tokens": 10,
                    "total_output_tokens": 4,
                },
            ),
            "visual_remount": StagedStageState(
                stage="visual_remount",
                status="noop",
                usage={},
            ),
        },
    )

    def fake_run(**kwargs):
        return state

    monkeypatch.setattr(cli, "run_staged_article_completion", fake_run)
    code = cli.main(
        [
            "--inputs-json",
            str(inputs_path),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["stage_model_tiers"] == {
        "conclusion": "c_model",
        "whole_manuscript_review": "c2_model",
    }
    assert summary["total_input_tokens"] == 15
    assert summary["total_output_tokens"] == 6


def test_aggregate_preserves_normalized_issue_identity() -> None:
    def finding(
        *,
        issue_type: str = "",
        target_ids: list[str] | None = None,
        issue_key: str = "",
        statement: str = "",
    ) -> ManuscriptReviewFinding:
        return ManuscriptReviewFinding(
            finding_id="",
            issue_key=issue_key,
            issue_type=issue_type,
            target_ids=target_ids or [],
            dimension="continuity",
            severity="advisory",
            consensus="none",
            statement=statement,
        )

    typed = MultiReviewerReport(
        reviewers=[
            ReviewerRole(
                reviewer_id="r1",
                role="continuity",
                findings=[
                    finding(
                        issue_type="missing_transition",
                        target_ids=["S02", "S01"],
                        statement="prose one",
                    )
                ],
            ),
            ReviewerRole(
                reviewer_id="r2",
                role="reader_flow",
                findings=[
                    finding(
                        issue_type="missing_transition",
                        target_ids=["S01", "S02"],
                        statement="prose two",
                    )
                ],
            ),
        ]
    )
    aggregated = aggregate_multi_reviewer_report(typed)
    assert len(aggregated.findings) == 1
    agg_finding = aggregated.findings[0]
    assert agg_finding.issue_type == "missing_transition"
    assert agg_finding.target_ids == ["S01", "S02"]
    assert agg_finding.issue_key == "missing_transition:S01,S02"
    assert agg_finding.finding_id == "GMR-001"

    fallback = MultiReviewerReport(
        reviewers=[
            ReviewerRole(
                reviewer_id="r1",
                role="logic",
                findings=[finding(issue_key="flow-gap", statement="one")],
            ),
            ReviewerRole(
                reviewer_id="r2",
                role="logic",
                findings=[finding(issue_key="flow-gap", statement="two")],
            ),
        ]
    )
    fallback_aggregated = aggregate_multi_reviewer_report(fallback)
    assert len(fallback_aggregated.findings) == 1
    assert fallback_aggregated.findings[0].issue_key == "flow-gap"
    assert fallback_aggregated.findings[0].issue_type == ""
    assert fallback_aggregated.findings[0].target_ids == []


def test_blocking_requires_all_critical_consensus() -> None:
    def finding(severity: str, statement: str) -> ManuscriptReviewFinding:
        return ManuscriptReviewFinding(
            finding_id="",
            issue_type="logic_conflict",
            target_ids=["S01"],
            dimension="logic",
            severity=severity,
            consensus="none",
            statement=statement,
        )

    split = MultiReviewerReport(
        reviewers=[
            ReviewerRole(
                reviewer_id="r1",
                role="logic",
                findings=[finding("critical", "wording one")],
            ),
            ReviewerRole(
                reviewer_id="r2",
                role="logic",
                findings=[finding("major", "wording two")],
            ),
        ]
    )
    aggregated = aggregate_multi_reviewer_report(split)
    assert len(aggregated.findings) == 1
    split_finding = aggregated.findings[0]
    assert split_finding.severity == "critical"
    assert split_finding.consensus == "consensus"
    assert split_finding.blocking is False
    assert split_finding.advisory is True
    assert aggregated.fail_open is True

    all_critical = MultiReviewerReport(
        reviewers=[
            ReviewerRole(
                reviewer_id="r1",
                role="logic",
                findings=[finding("critical", "wording one")],
            ),
            ReviewerRole(
                reviewer_id="r2",
                role="logic",
                findings=[finding("critical", "wording two")],
            ),
        ]
    )
    blocked = aggregate_multi_reviewer_report(all_critical)
    assert len(blocked.findings) == 1
    assert blocked.findings[0].blocking is True
    assert blocked.findings[0].advisory is False
    assert blocked.fail_open is False

    partial = MultiReviewerReport(
        reviewers=[
            ReviewerRole(
                reviewer_id="r1",
                role="logic",
                findings=[finding("critical", "wording one")],
            ),
            ReviewerRole(reviewer_id="r2", role="logic", findings=[]),
            ReviewerRole(
                reviewer_id="r3",
                role="logic",
                findings=[finding("critical", "wording three")],
            ),
        ]
    )
    partial_aggregated = aggregate_multi_reviewer_report(partial)
    assert len(partial_aggregated.findings) == 1
    assert partial_aggregated.findings[0].consensus == "majority"
    assert partial_aggregated.findings[0].blocking is False
    assert partial_aggregated.findings[0].advisory is True
    assert partial_aggregated.fail_open is True


def test_multi_reviewer_provider_local_canonical_identity_override(
    monkeypatch,
) -> None:
    def fake_call_qwen_chat(*args, **kwargs):
        return {
            "content": json.dumps(
                {
                    "findings": [
                        {
                            "finding_id": "model-id",
                            "issue_key": "model-free-text",
                            "issue_type": "ordering_break",
                            "target_ids": ["S2", "S1"],
                            "dimension": "reader_flow",
                            "severity": "critical",
                            "consensus": "consensus",
                            "statement": "same ordering break",
                            "blocking": True,
                        }
                    ]
                }
            ),
            "_llm_usage": {},
        }

    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call_qwen_chat)
    provider = make_multi_reviewer_qwen_provider(
        reviewers=[
            {"reviewer_id": "r1", "role": "reader_flow"},
            {"reviewer_id": "r2", "role": "reader_flow"},
        ],
        qwen_call=fake_call_qwen_chat,
    )
    output = provider({"stage": "whole_manuscript_review", "inputs": {}})
    review = output["review"]
    assert len(review["findings"]) == 1
    aggregated = review["findings"][0]
    assert aggregated["issue_key"] == "ordering_break:S1,S2"
    assert aggregated["issue_type"] == "ordering_break"
    assert aggregated["target_ids"] == ["S1", "S2"]
    assert aggregated["blocking"] is True
    assert aggregated["advisory"] is False
    assert aggregated["finding_id"] == "GMR-001"
    for role in output["reviewers"]:
        stored = role["findings"][0]
        assert stored["issue_type"] == "ordering_break"
        assert stored["target_ids"] == ["S2", "S1"]
        assert stored["issue_key"] == ""
        assert stored["finding_id"] == ""
        assert stored["consensus"] == "none"
        assert stored["blocking"] is False


def test_consensus_counts_distinct_reviewers_not_duplicates() -> None:
    def finding(severity: str, statement: str) -> ManuscriptReviewFinding:
        return ManuscriptReviewFinding(
            finding_id="",
            issue_type="logic_conflict",
            target_ids=["S01"],
            dimension="logic",
            severity=severity,
            consensus="none",
            statement=statement,
        )

    solo_duplicate = MultiReviewerReport(
        reviewers=[
            ReviewerRole(
                reviewer_id="r1",
                role="logic",
                findings=[
                    finding("advisory", "wording one"),
                    finding("critical", "wording two"),
                ],
            ),
        ]
    )
    solo = aggregate_multi_reviewer_report(solo_duplicate)
    assert len(solo.findings) == 1
    # duplicate same-reviewer findings merge to one vote: higher severity is
    # kept but a single reviewer can never form consensus or block.
    assert solo.findings[0].severity == "critical"
    assert solo.findings[0].consensus == "majority"
    assert solo.findings[0].blocking is False
    assert solo.findings[0].advisory is True
    assert solo.fail_open is True

    consensus_with_duplicate = MultiReviewerReport(
        reviewers=[
            ReviewerRole(
                reviewer_id="r1",
                role="logic",
                findings=[
                    finding("critical", "wording one"),
                    finding("critical", "wording two"),
                ],
            ),
            ReviewerRole(
                reviewer_id="r2",
                role="logic",
                findings=[finding("critical", "wording three")],
            ),
        ]
    )
    consensus = aggregate_multi_reviewer_report(consensus_with_duplicate)
    assert len(consensus.findings) == 1
    assert consensus.findings[0].consensus == "consensus"
    assert consensus.findings[0].blocking is True
    assert consensus.findings[0].advisory is False
    assert consensus.fail_open is False

    blank_ids = MultiReviewerReport(
        reviewers=[
            ReviewerRole(
                reviewer_id="",
                role="logic",
                findings=[finding("critical", "wording one")],
            ),
            ReviewerRole(
                reviewer_id="",
                role="logic",
                findings=[finding("critical", "wording two")],
            ),
        ]
    )
    blank_aggregated = aggregate_multi_reviewer_report(blank_ids)
    assert len(blank_aggregated.findings) == 1
    assert blank_aggregated.findings[0].consensus == "consensus"
    assert blank_aggregated.findings[0].blocking is True

    duplicate_ids = MultiReviewerReport(
        reviewers=[
            ReviewerRole(
                reviewer_id="same",
                role="logic",
                findings=[finding("critical", "wording one")],
            ),
            ReviewerRole(
                reviewer_id="same",
                role="logic",
                findings=[finding("critical", "wording two")],
            ),
        ]
    )
    dup_aggregated = aggregate_multi_reviewer_report(duplicate_ids)
    assert len(dup_aggregated.findings) == 1
    assert dup_aggregated.findings[0].consensus == "consensus"
    assert dup_aggregated.findings[0].blocking is True


def test_editorial_revision_offline_assembles_manuscript(
    tmp_path: Path,
) -> None:
    def draft_provider(name: str):
        def provider(_stage_input: Mapping[str, Any]) -> dict[str, Any]:
            return {
                "workplan": {"derived_from": []},
                "draft": {"text": f"{name} text"},
                "status": "drafted",
            }

        return provider

    providers = {
        "conclusion": draft_provider("conclusion"),
        "introduction": draft_provider("introduction"),
        "abstract": draft_provider("abstract"),
    }
    state = run_staged_article_completion(
        work_dir=tmp_path / "out",
        inputs={
            "sections": [
                {
                    "section_id": "S01",
                    "section_title": "First Section",
                    "blocks": [
                        {"block_id": "S01-B001", "prose": "first block"},
                    ],
                }
            ]
        },
        stage_providers=providers,
        run_id="er-offline",
    )
    artifact = json.loads(
        (tmp_path / "out" / "staged_editorial_revision.json").read_text(
            encoding="utf-8"
        )
    )
    payload = artifact["payload"]
    assert payload["status"] == "offline_noop"
    manuscript = payload["manuscript"]
    assert [entry["kind"] for entry in manuscript["assembled"]] == [
        "abstract",
        "introduction",
        "section",
        "conclusion",
    ]
    assert "abstract text" in manuscript["full_text"]
    assert "introduction text" in manuscript["full_text"]
    assert "conclusion text" in manuscript["full_text"]
    assert "first block" in manuscript["full_text"]
    audit = payload["audit"]
    assert audit["work_item_count"] == 0
    assert audit["accepted_count"] == 0
    assert audit["blocking_unresolved"] == []
    assert state.stages["editorial_revision"].status == "completed"


def test_editorial_revision_mocked_author_verifier_flow(
    monkeypatch,
) -> None:
    fake_call, calls = _make_editorial_qwen_mock(
        revised_map={
            "Old transition": "Smoothed transition [REF:p1].",
            "Jargon term": "The clarified term is explained [REF:p3].",
        }
    )
    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call)
    findings = [
        _editorial_finding(
            finding_id="F-1",
            issue_type="ordering_break",
            target_ids=["S01-B001"],
            severity="major",
            consensus="consensus",
            dimension="reader_flow",
            statement="awkward transition",
        ),
        _editorial_finding(
            finding_id="F-2",
            issue_type="undefined_term",
            target_ids=["S01-B003"],
            severity="major",
            consensus="majority",
            dimension="clarity",
            statement="undefined term",
        ),
    ]
    provider = make_editorial_revision_qwen_provider(qwen_call=fake_call)
    output = provider(_editorial_stage_input(findings=findings))

    author_calls = [
        call
        for call in calls
        if call["agent"] == "StagedEditorialRevisionAuthor"
    ]
    verifier_calls = [
        call
        for call in calls
        if call["agent"] == "StagedEditorialRevisionVerifier"
    ]
    assert len(author_calls) == 2
    assert len(verifier_calls) == 2

    first_user = json.loads(author_calls[0]["messages"][1]["content"])[
        "stage_input"
    ]
    assert first_user["original_text"] == "Old transition here [REF:p1]."
    assert first_user["next_text"] == "Middle block content."
    assert first_user["previous_text"] == ""
    assert "Jargon term needs clarification" not in json.dumps(first_user)
    assert "conclusion text" not in json.dumps(first_user)

    second_user = json.loads(author_calls[1]["messages"][1]["content"])[
        "stage_input"
    ]
    assert second_user["original_text"] == (
        "Jargon term needs clarification [REF:p3]."
    )
    assert "Old transition here" not in json.dumps(second_user)

    section = output["manuscript"]["assembled"][2]
    by_block = {block["block_id"]: block for block in section["blocks"]}
    assert by_block["S01-B001"]["text"] == "Smoothed transition [REF:p1]."
    assert by_block["S01-B001"]["revised"] is True
    assert by_block["S01-B001"]["original_sha256"] != by_block[
        "S01-B001"
    ]["revised_sha256"]
    assert by_block["S01-B002"]["text"] == "Middle block content."
    assert by_block["S01-B003"]["text"] == (
        "The clarified term is explained [REF:p3]."
    )
    assert "Smoothed transition" in output["manuscript"]["full_text"]
    assert "conclusion text" in output["manuscript"]["full_text"]

    audit = output["audit"]
    assert audit["work_item_count"] == 2
    assert audit["accepted_count"] == 2
    assert audit["rejected_count"] == 0
    records = {record["work_item_id"]: record for record in audit["records"]}
    assert records["ER-001"]["status"] == "accepted"
    assert records["ER-001"]["source_finding"] == "ordering_break:S01-B001"
    assert records["ER-001"]["original_sha256"]
    assert records["ER-001"]["revised_sha256"]
    assert records["ER-001"]["author_usage"]["input_tokens"] == 3
    assert records["ER-001"]["verifier_usage"]["input_tokens"] == 2
    assert audit["review_findings_kept"] == findings

    usage = output["_usage"]
    assert usage["model_tier"] == "c_model"
    assert usage["verifier_model_tier"] == "c2_model"
    assert usage["author_call_count"] == 2
    assert usage["verifier_call_count"] == 2
    assert usage["total_input_tokens"] == 10
    assert usage["total_output_tokens"] == 4


def test_editorial_revision_ref_marker_mutation_rejected(
    monkeypatch,
) -> None:
    fake_call, calls = _make_editorial_qwen_mock(
        revised_map={
            "Old transition": "Smoothed transition without its ref marker."
        }
    )
    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call)
    findings = [
        _editorial_finding(
            finding_id="F-1",
            issue_type="ordering_break",
            target_ids=["S01-B001"],
            severity="major",
        )
    ]
    provider = make_editorial_revision_qwen_provider(qwen_call=fake_call)
    output = provider(_editorial_stage_input(findings=findings))

    assert output["audit"]["work_item_count"] == 1
    assert output["audit"]["accepted_count"] == 0
    record = output["audit"]["records"][0]
    assert record["status"] == "rejected"
    assert record["reason"] == "ref_markers_changed"
    block = output["manuscript"]["assembled"][2]["blocks"][0]
    assert block["text"] == "Old transition here [REF:p1]."
    assert block["revised"] is False
    verifier_calls = [
        call
        for call in calls
        if call["agent"] == "StagedEditorialRevisionVerifier"
    ]
    assert verifier_calls == []
    assert output["_usage"]["verifier_call_count"] == 0


def test_editorial_revision_verifier_rejection_fail_open(
    monkeypatch,
) -> None:
    fake_call, calls = _make_editorial_qwen_mock(
        verifier_ok=False,
        revised_map={"Old transition": "Smoothed transition [REF:p1]."},
    )
    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call)
    findings = [
        _editorial_finding(
            finding_id="F-1",
            issue_type="ordering_break",
            target_ids=["S01-B001"],
            severity="major",
        )
    ]
    provider = make_editorial_revision_qwen_provider(qwen_call=fake_call)
    output = provider(_editorial_stage_input(findings=findings))

    assert output["audit"]["work_item_count"] == 1
    assert output["audit"]["accepted_count"] == 0
    assert output["audit"]["rejected_count"] == 1
    record = output["audit"]["records"][0]
    assert record["status"] == "rejected"
    assert record["reason"].startswith("verifier_rejected:")
    block = output["manuscript"]["assembled"][2]["blocks"][0]
    assert block["text"] == "Old transition here [REF:p1]."
    assert block["revised"] is False
    verifier_calls = [
        call
        for call in calls
        if call["agent"] == "StagedEditorialRevisionVerifier"
    ]
    assert len(verifier_calls) == 1


def test_editorial_revision_upstream_files_byte_identical(
    tmp_path: Path, monkeypatch
) -> None:
    chapter = tmp_path / "chapter_S01.md"
    original_bytes = "Old transition here [REF:p1].\n".encode("utf-8")
    chapter.write_bytes(original_bytes)
    fake_call, _ = _make_editorial_qwen_mock(
        revised_map={"Old transition": "Smoothed transition [REF:p1]."}
    )
    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call)
    provider = make_editorial_revision_qwen_provider(qwen_call=fake_call)
    stage_input = _editorial_stage_input(
        findings=[
            _editorial_finding(
                finding_id="F-1",
                issue_type="ordering_break",
                target_ids=["S01-B001"],
                severity="major",
            )
        ]
    )
    stage_input["inputs"]["sections"][0]["provenance"] = {
        "enhanced_chapter": str(chapter)
    }
    output = provider(stage_input)
    assert output["audit"]["accepted_count"] == 1
    assert chapter.read_bytes() == original_bytes


def test_editorial_revision_resume_noop(tmp_path: Path, monkeypatch) -> None:
    fake_call, calls = _make_editorial_qwen_mock(
        revised_map={"Old transition": "Smoothed transition [REF:p1]."}
    )
    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call)
    finding = _editorial_finding(
        finding_id="F-1",
        issue_type="ordering_break",
        target_ids=["S01-B001"],
        severity="major",
    )
    inputs = {
        "sections": [
            {
                "section_id": "S01",
                "section_title": "First Section",
                "blocks": [
                    {"block_id": "S01-B001", "prose": "Old transition here [REF:p1]."},
                ],
            }
        ]
    }

    def finding_provider(_stage_input):
        return {"review": {"findings": [finding], "fail_open": True}, "status": "reviewed"}

    def patches_provider(_stage_input):
        return {"proposals": [], "approval_required": False}

    providers = {
        "whole_manuscript_review": finding_provider,
        "bounded_patch_proposals": patches_provider,
        "editorial_revision": make_editorial_revision_qwen_provider(
            qwen_call=fake_call
        ),
    }
    run_kwargs = dict(
        work_dir=tmp_path / "out",
        inputs=inputs,
        stage_providers=providers,
        run_id="resume-er",
    )
    first = run_staged_article_completion(**run_kwargs)
    assert first.stages["editorial_revision"].status == "completed"
    assert first.stages["editorial_revision"].payload["audit"][
        "accepted_count"
    ] == 1
    calls.clear()
    second = run_staged_article_completion(**run_kwargs, resume=True)
    assert second.stages["editorial_revision"].status == "noop"
    assert calls == []


def test_editorial_revision_critical_unresolved_fail_open(
    tmp_path: Path, monkeypatch
) -> None:
    fake_call, _ = _make_editorial_qwen_mock(
        revised_map={"Jargon term": "The clarified term is explained [REF:p3]."}
    )
    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call)
    unresolved = _editorial_finding(
        finding_id="CRIT-1",
        issue_type="logic_conflict",
        target_ids=[],
        severity="critical",
        consensus="consensus",
        statement="critical issue with no safe target",
    )
    ordinary = _editorial_finding(
        finding_id="F-2",
        issue_type="undefined_term",
        target_ids=["S01-B003"],
        severity="major",
        consensus="majority",
        dimension="clarity",
        statement="undefined term",
    )
    inputs = {
        "sections": [
            {
                "section_id": "S01",
                "section_title": "First Section",
                "blocks": [
                    {"block_id": "S01-B003", "prose": "Jargon term needs clarification [REF:p3]."},
                ],
            }
        ]
    }

    def finding_provider(_stage_input):
        return {
            "review": {
                "findings": [unresolved, ordinary],
                "fail_open": True,
            },
            "status": "reviewed",
        }

    def patches_provider(_stage_input):
        return {"proposals": [], "approval_required": False}

    state = run_staged_article_completion(
        work_dir=tmp_path / "out",
        inputs=inputs,
        stage_providers={
            "whole_manuscript_review": finding_provider,
            "bounded_patch_proposals": patches_provider,
            "editorial_revision": make_editorial_revision_qwen_provider(
                qwen_call=fake_call
            ),
        },
        run_id="critical-er",
    )
    assert state.status == "completed"
    assert state.awaiting_approval_stages == []
    payload = state.stages["editorial_revision"].payload
    audit = payload["audit"]
    assert audit["blocking_unresolved"] == [
        {
            "finding_id": "CRIT-1",
            "issue_key": "logic_conflict:",
            "severity": "critical",
            "statement": "critical issue with no safe target",
            "reason": "no_materializable_auto_target",
        }
    ]
    assert audit["review_findings_kept"] == [unresolved, ordinary]
    assert audit["accepted_count"] == 1


def test_plan_editorial_work_items_bounded_and_deduped() -> None:
    sections = [
        {
            "section_id": "S01",
            "section_title": "First Section",
            "blocks": [
                {"block_id": "S01-B001", "prose": "a"},
                {"block_id": "S01-B002", "prose": "b"},
            ],
        },
        {
            "section_id": "S02",
            "section_title": "Second Section",
            "blocks": [
                {"block_id": "S02-B001", "prose": "c"},
                {"block_id": "S02-B002", "prose": "d"},
            ],
        },
    ]
    findings = [
        _editorial_finding(
            finding_id="F-1",
            issue_type="ordering_break",
            target_ids=["S01"],
            severity="advisory",
        ),
        _editorial_finding(
            finding_id="F-2",
            issue_type="logic_conflict",
            target_ids=["S02-B001"],
            severity="critical",
            consensus="consensus",
            dimension="logic",
        ),
        _editorial_finding(
            finding_id="F-3",
            issue_type="duplicated_explanation",
            target_ids=["S02-B002"],
            severity="major",
            dimension="overlap",
        ),
        _editorial_finding(
            finding_id="F-4",
            issue_type="duplicated_explanation",
            target_ids=["S02-B002"],
            severity="major",
            dimension="overlap",
            statement="same overlap wording",
        ),
    ]
    proposals = [
        {
            "patch_id": "P-1",
            "operation": "normalize_reference",
            "target": "S01-B002",
            "rationale": "duplicate wording",
            "approval_required": False,
        },
        {
            "patch_id": "P-2",
            "operation": "rewrite_transition",
            "target": "S01-B001",
            "rationale": "commander safe transition",
            "approval_required": True,
        },
    ]
    work_items, unresolved = plan_editorial_work_items(
        sections=sections,
        front_back={},
        findings=findings,
        proposals=proposals,
    )
    by_target = {
        item.target_block_id: item for item in work_items
    }
    # Commander-emitted pure-editorial transition op is materialized; the
    # approval flag does not affect work-item planning.
    assert by_target["S01-B001"].editorial_kind == "transition"
    assert by_target["S01-B001"].patch_proposal["patch_id"] == "P-2"
    # normalize_reference is deterministic elsewhere and never routed here.
    assert "S01-B002" not in by_target
    # logic_conflict is never auto-edited: recorded for manual handling.
    assert "S02-B001" not in by_target
    # duplicate findings on the same canonical issue + target merge to one
    # bounded item (first source retained).
    assert by_target["S02-B002"].finding["finding_id"] == "F-3"
    assert by_target["S02-B002"].editorial_kind == "cross_reference"
    # single-section transition with no prior section remains advisory.
    assert unresolved == [
        {
            "finding_id": "F-2",
            "issue_key": "logic_conflict:S02-B001",
            "severity": "critical",
            "statement": "editorial note",
            "reason": "no_materializable_auto_target",
        }
    ]
    assert [item.work_item_id for item in work_items] == [
        "ER-001",
        "ER-002",
    ]


def test_plan_transition_boundary_single_edit() -> None:
    sections = [
        {
            "section_id": "S01",
            "section_title": "One",
            "blocks": [
                {"block_id": "S01-B001", "prose": "a"},
                {"block_id": "S01-B002", "prose": "b"},
            ],
        },
        {
            "section_id": "S02",
            "section_title": "Two",
            "blocks": [
                {"block_id": "S02-B001", "prose": "c"},
                {"block_id": "S02-B002", "prose": "d"},
            ],
        },
    ]
    findings = [
        _editorial_finding(
            finding_id="F-1",
            issue_type="missing_transition",
            target_ids=["S01", "S02"],
            severity="major",
            consensus="consensus",
            dimension="continuity",
        )
    ]
    work_items, unresolved = plan_editorial_work_items(
        sections=sections,
        front_back={},
        findings=findings,
        proposals=[],
    )
    assert unresolved == []
    assert len(work_items) == 1
    item = work_items[0]
    assert item.target_block_id == "S02-B001"
    assert item.previous_text == "b"
    assert item.editorial_kind == "transition"

    reversed_findings = [
        _editorial_finding(
            finding_id="F-2",
            issue_type="missing_transition",
            target_ids=["S02", "S01"],
            severity="major",
            consensus="consensus",
            dimension="continuity",
        )
    ]
    work_items_reversed, _ = plan_editorial_work_items(
        sections=sections,
        front_back={},
        findings=reversed_findings,
        proposals=[],
    )
    assert len(work_items_reversed) == 1
    assert work_items_reversed[0].target_block_id == "S02-B001"
    assert work_items_reversed[0].previous_text == "b"


def test_editorial_revision_multi_target_findings_single_item() -> None:
    sections = [
        {
            "section_id": "S01",
            "section_title": "One",
            "blocks": [
                {"block_id": "S01-B001", "prose": "a1"},
                {"block_id": "S01-B002", "prose": "a2"},
                {"block_id": "S01-B003", "prose": "a3"},
            ],
        },
        {
            "section_id": "S02",
            "section_title": "Two",
            "blocks": [
                {"block_id": "S02-B001", "prose": "b1"},
                {"block_id": "S02-B002", "prose": "b2"},
            ],
        },
    ]
    findings = [
        _editorial_finding(
            finding_id="DUP-1",
            issue_type="duplicated_explanation",
            target_ids=["S01-B001", "S02-B001", "S02-B002", "S01-B003"],
            severity="major",
            dimension="overlap",
        ),
        _editorial_finding(
            finding_id="TERM-1",
            issue_type="undefined_term",
            target_ids=["S02-B002", "S01-B002", "S01-B001"],
            severity="major",
            dimension="clarity",
        ),
    ]
    proposals = [
        {
            "patch_id": "P-1",
            "operation": "rewrite_transition",
            "target": "S02-B001",
            "rationale": "commander transition",
            "approval_required": False,
        }
    ]
    work_items, unresolved = plan_editorial_work_items(
        sections=sections,
        front_back={},
        findings=findings,
        proposals=proposals,
    )
    assert unresolved == []
    # items are bounded by findings + proposals, never by target count.
    assert len(work_items) == 3
    dup = next(
        item
        for item in work_items
        if item.finding.get("finding_id") == "DUP-1"
    )
    assert dup.target_block_id == "S02-B002"  # latest body target
    assert dup.editorial_kind == "cross_reference"
    term = next(
        item
        for item in work_items
        if item.finding.get("finding_id") == "TERM-1"
    )
    assert term.target_block_id == "S01-B001"  # earliest body target
    assert term.editorial_kind == "terminology_clarification"
    by_target = {item.target_block_id: item for item in work_items}
    assert by_target["S02-B001"].patch_proposal["patch_id"] == "P-1"


def test_editorial_revision_items_bounded_by_findings_not_targets() -> None:
    sections = [
        {
            "section_id": "S01",
            "section_title": "One",
            "blocks": [
                {"block_id": "S01-B001", "prose": "a1"},
                {"block_id": "S01-B002", "prose": "a2"},
                {"block_id": "S01-B003", "prose": "a3"},
                {"block_id": "S01-B004", "prose": "a4"},
            ],
        }
    ]
    findings = [
        _editorial_finding(
            finding_id="F-1",
            issue_type="duplicated_explanation",
            target_ids=[
                "S01-B001",
                "S01-B002",
                "S01-B003",
                "S01-B004",
            ],
            severity="major",
            dimension="overlap",
        ),
        _editorial_finding(
            finding_id="F-2",
            issue_type="duplicated_explanation",
            target_ids=["S01-B001", "S01-B002", "S01-B003"],
            severity="major",
            dimension="overlap",
        ),
        _editorial_finding(
            finding_id="F-3",
            issue_type="duplicated_explanation",
            target_ids=["S01-B001", "S01-B002"],
            severity="major",
            dimension="overlap",
        ),
    ]
    work_items, unresolved = plan_editorial_work_items(
        sections=sections,
        front_back={},
        findings=findings,
        proposals=[],
    )
    assert unresolved == []
    assert len(work_items) == 3  # 9 referenced targets, but 3 findings
    assert {item.target_block_id for item in work_items} == {
        "S01-B004",
        "S01-B003",
        "S01-B002",
    }


def test_editorial_revision_provider_multi_target_single_item(
    monkeypatch,
) -> None:
    fake_call, calls = _make_editorial_qwen_mock(
        revised_map={
            "block one": "block one with term defined [REF:p1].",
            "block two": "block two cross-reference [REF:p2].",
        }
    )
    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call)
    findings = [
        _editorial_finding(
            finding_id="DUP-1",
            issue_type="duplicated_explanation",
            target_ids=["S01-B001", "S01-B002"],
            severity="major",
            dimension="overlap",
        ),
        _editorial_finding(
            finding_id="TERM-1",
            issue_type="undefined_term",
            target_ids=["S01-B003", "S01-B002", "S01-B001"],
            severity="major",
            dimension="clarity",
        ),
    ]
    stage_input = _editorial_stage_input(
        sections=[
            {"block_id": "S01-B001", "prose": "block one [REF:p1]."},
            {"block_id": "S01-B002", "prose": "block two [REF:p2]."},
            {"block_id": "S01-B003", "prose": "block three [REF:p3]."},
        ],
        findings=findings,
    )
    provider = make_editorial_revision_qwen_provider(qwen_call=fake_call)
    output = provider(stage_input)
    audit = output["audit"]
    assert audit["work_item_count"] == 2
    assert audit["accepted_count"] == 2
    assert [record["target_block_id"] for record in audit["records"]] == [
        "S01-B001",
        "S01-B002",
    ]
    assert [record["editorial_kind"] for record in audit["records"]] == [
        "terminology_clarification",
        "cross_reference",
    ]
    author_calls = [
        call
        for call in calls
        if call["agent"] == "StagedEditorialRevisionAuthor"
    ]
    assert len(author_calls) == 2
    blocks = {
        block["block_id"]: block
        for block in output["manuscript"]["assembled"][2]["blocks"]
    }
    assert blocks["S01-B002"]["text"] == (
        "block two cross-reference [REF:p2]."
    )
    assert blocks["S01-B001"]["text"] == (
        "block one with term defined [REF:p1]."
    )
    assert blocks["S01-B003"]["text"] == "block three [REF:p3]."


def test_assemble_revised_manuscript_applies_only_accepted() -> None:
    manuscript = assemble_revised_manuscript(
        sections=[
            {
                "section_id": "S01",
                "section_title": "First Section",
                "blocks": [
                    {"block_id": "S01-B001", "prose": "original one [REF:p1]."},
                    {"block_id": "S01-B002", "prose": "original two [REF:p2]."},
                ],
            }
        ],
        front_back={
            "abstract": "abstract draft",
            "introduction": "introduction draft",
            "conclusion": "conclusion draft",
        },
        revisions={"S01-B001": "revised one [REF:p1]."},
    )
    assert [entry["kind"] for entry in manuscript["assembled"]] == [
        "abstract",
        "introduction",
        "section",
        "conclusion",
    ]
    blocks = manuscript["assembled"][2]["blocks"]
    assert blocks[0]["text"] == "revised one [REF:p1]."
    assert blocks[0]["revised"] is True
    assert blocks[1]["text"] == "original two [REF:p2]."
    assert blocks[1]["revised"] is False
    assert "revised one" in manuscript["full_text"]
    assert "original two" in manuscript["full_text"]
    assert "abstract draft" in manuscript["full_text"]
    assert ref_markers_preserved(
        "one [REF:p1] here", "one [REF:p1] and another [REF:p1]"
    ) is False
    assert ref_markers_preserved(
        "one [REF:p1] here", "one [REF:p1] moved here"
    ) is True
    assert verifier_accepts(
        {
            "meaning_preserved": True,
            "scope_preserved": True,
            "citations_preserved": True,
            "numbers_conditions_preserved": True,
            "problem_improved": True,
        }
    ) is True
    assert verifier_accepts(
        {
            "meaning_preserved": True,
            "scope_preserved": True,
            "citations_preserved": True,
            "numbers_conditions_preserved": True,
            "problem_improved": False,
        }
    ) is False


def test_cli_editorial_verifier_tier_flag_and_validation(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(json.dumps({"topic": "x"}), encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return _cli_fake_state()

    monkeypatch.setattr(cli, "run_staged_article_completion", fake_run)
    code = cli.main(
        [
            "--inputs-json",
            str(inputs_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--live-stages",
            "editorial_revision",
            "--editorial-verifier-tier",
            "b_model",
        ]
    )
    assert code == 0
    provider = captured["stage_providers"]["editorial_revision"]
    assert isinstance(provider, QwenEditorialRevisionProvider)
    assert provider.model_tier == "c_model"
    assert provider.verifier_tier == "b_model"

    code_invalid = cli.main(
        [
            "--inputs-json",
            str(inputs_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--editorial-verifier-tier",
            "not_a_tier",
        ]
    )
    assert code_invalid == 2
    assert (
        "unsupported editorial verifier tier" in capsys.readouterr().err
    )


def test_ref_marker_order_changes_rejected(monkeypatch) -> None:
    assert ref_markers_preserved(
        "[REF:p1] [REF:p2]", "[REF:p1] [REF:p2]"
    ) is True
    assert ref_markers_preserved(
        "[REF:p1] [REF:p2]", "[REF:p2] [REF:p1]"
    ) is False

    fake_call, calls = _make_editorial_qwen_mock(
        revised_map={
            "Old transition": "Smoothed transition [REF:p2] [REF:p1]."
        }
    )
    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call)
    findings = [
        _editorial_finding(
            finding_id="F-1",
            issue_type="ordering_break",
            target_ids=["S01-B001"],
            severity="major",
        )
    ]
    stage_input = _editorial_stage_input(
        sections=[
            {
                "block_id": "S01-B001",
                "prose": "Old transition [REF:p1] [REF:p2].",
            },
            {"block_id": "S01-B002", "prose": "Middle block content."},
            {
                "block_id": "S01-B003",
                "prose": "Jargon term needs clarification [REF:p3].",
            },
        ],
        findings=findings,
    )
    provider = make_editorial_revision_qwen_provider(qwen_call=fake_call)
    output = provider(stage_input)
    assert output["audit"]["accepted_count"] == 0
    record = output["audit"]["records"][0]
    assert record["status"] == "rejected"
    assert record["reason"] == "ref_markers_changed"
    verifier_calls = [
        call
        for call in calls
        if call["agent"] == "StagedEditorialRevisionVerifier"
    ]
    assert verifier_calls == []


def test_editorial_revision_commander_order_headings_and_full_text_authority(
    tmp_path: Path,
) -> None:
    sections = [
        {
            "section_id": "S02",
            "section_title": "Second",
            "full_text": "Section two full text.",
            "blocks": [
                {"block_id": "S02-B001", "prose": "Section two full text."}
            ],
        },
        {
            "section_id": "S01",
            "section_title": "First",
            "full_text": "Opening [REF:p1].\n\nTrailing prose not in blocks.",
            "blocks": [
                {"block_id": "S01-B001", "prose": "Opening [REF:p1]."}
            ],
        },
    ]
    manuscript = assemble_revised_manuscript(
        sections=sections,
        front_back={
            "abstract": "abstract draft",
            "introduction": "introduction draft",
            "conclusion": "conclusion draft",
        },
        revisions={"S01-B001": "Revised opening [REF:p1]."},
        section_order=["S01", "S02"],
    )
    entries = manuscript["assembled"]
    assert [entry["heading"] for entry in entries] == [
        "Abstract",
        "Introduction",
        "First",
        "Second",
        "Conclusion",
    ]
    for heading in ("Abstract", "Introduction", "First", "Second", "Conclusion"):
        assert manuscript["full_text"].count("## " + heading) == 1
    first = entries[2]
    assert first["full_text_authority"] is True
    assert "Trailing prose not in blocks" in first["text"]
    assert "Revised opening [REF:p1]." in first["text"]
    assert "## First" in manuscript["full_text"]
    assert manuscript["full_text"].index("## First") < manuscript[
        "full_text"
    ].index("## Second")


def test_citation_free_block_revision_materializes_in_cited_full_text(
    tmp_path: Path, monkeypatch
) -> None:
    original_paragraph = (
        "Old transition here [REF:p1]. More evidence follows [REF:p2]."
    )
    revised_paragraph = (
        "A smoother transition starts here [REF:p1]. "
        "More evidence follows [REF:p2]."
    )
    original_full_text = "# First Section\n\n" + original_paragraph
    fake_call, calls = _make_editorial_qwen_mock(
        revised_map={"Old transition": revised_paragraph}
    )
    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call)
    finding = _editorial_finding(
        finding_id="F-RICH-1",
        issue_type="ordering_break",
        target_ids=["S01-B001"],
        severity="major",
        consensus="consensus",
        dimension="reader_flow",
    )
    inputs = {
        "sections": [
            {
                "section_id": "S01",
                "section_title": "First Section",
                "full_text": original_full_text,
                "blocks": [
                    {
                        "block_id": "S01-B001",
                        "prose": (
                            "Old transition here. More evidence follows."
                        ),
                    }
                ],
            }
        ]
    }

    def finding_provider(_stage_input):
        return {
            "review": {"findings": [finding], "fail_open": True},
            "status": "reviewed",
        }

    def patches_provider(_stage_input):
        return {"proposals": [], "approval_required": False}

    state = run_staged_article_completion(
        work_dir=tmp_path / "out",
        inputs=inputs,
        stage_providers={
            "whole_manuscript_review": finding_provider,
            "bounded_patch_proposals": patches_provider,
            "editorial_revision": make_editorial_revision_qwen_provider(
                qwen_call=fake_call
            ),
        },
        run_id="citation-rich-materialization",
    )
    payload = state.stages["editorial_revision"].payload
    manuscript = payload["manuscript"]
    section = next(
        entry
        for entry in manuscript["assembled"]
        if entry["target_id"] == "S01"
    )
    assert section["applied_block_revisions"] == ["S01-B001"]
    assert section["unapplied_block_revisions"] == []
    assert revised_paragraph in section["text"]
    assert original_paragraph not in section["text"]
    assert revised_paragraph in manuscript["full_text"]
    assert original_paragraph not in manuscript["full_text"]
    assert manuscript["applied_revision_ids"] == ["S01-B001"]
    assert manuscript["unapplied_revision_ids"] == []
    assert ref_markers_preserved(
        original_full_text, manuscript["full_text"]
    ) is True

    audit = payload["audit"]
    assert audit["accepted_count"] == 1
    assert audit["applied_revision_ids"] == ["ER-001"]
    assert audit["unapplied_accepted_revision_ids"] == []
    assert audit["records"][0]["application_status"] == "applied"

    author_call = next(
        call
        for call in calls
        if call["agent"] == "StagedEditorialRevisionAuthor"
    )
    verifier_call = next(
        call
        for call in calls
        if call["agent"] == "StagedEditorialRevisionVerifier"
    )
    author_input = json.loads(author_call["messages"][1]["content"])[
        "stage_input"
    ]
    verifier_input = json.loads(verifier_call["messages"][1]["content"])[
        "stage_input"
    ]
    assert author_input["original_text"] == original_paragraph
    assert verifier_input["original_text"] == original_paragraph
    assert verifier_input["revised_text"] == revised_paragraph

    staged_markdown = (
        tmp_path / "out" / "STAGED_COMPLETE_REVIEW_EN.md"
    ).read_text(encoding="utf-8")
    assert revised_paragraph in staged_markdown
    assert original_paragraph not in staged_markdown
    assert ref_markers_preserved(original_full_text, staged_markdown) is True

    from optomind_research.runtime.publication_mainline_adapter import (
        _build_downstream_section_dir,
    )

    downstream = _build_downstream_section_dir(
        staged_work_dir=tmp_path / "out",
        staged_state=state,
        output_dir=tmp_path / "downstream",
    )
    downstream_text = (
        downstream / "S01" / "SECTION_DRAFT_EN.md"
    ).read_text(encoding="utf-8")
    assert revised_paragraph in downstream_text
    assert original_paragraph not in downstream_text
    assert ref_markers_preserved(original_paragraph, downstream_text) is True


def test_assemble_deduplicates_matching_leading_markdown_headings() -> None:
    manuscript = assemble_revised_manuscript(
        sections=[
            {
                "section_id": "S01",
                "section_title": "First Section",
                "full_text": "# First Section\n\nBody [REF:p1].",
                "blocks": [
                    {
                        "block_id": "S01-B001",
                        "prose": "Body [REF:p1].",
                    }
                ],
            }
        ],
        front_back={
            "abstract": "# Abstract\n\nAbstract body.",
            "introduction": "## Introduction\n\nIntroduction body.",
            "conclusion": "# Conclusion\n\nConclusion body.",
        },
        revisions={},
    )
    for heading in ("Abstract", "Introduction", "First Section", "Conclusion"):
        assert len(
            re.findall(
                rf"(?m)^#{{1,6}}[ \t]+{re.escape(heading)}[ \t]*$",
                manuscript["full_text"],
            )
        ) == 1
    assert not re.search(
        r"(?m)^#[ \t]+First Section[ \t]*$", manuscript["full_text"]
    )
    section = next(
        entry
        for entry in manuscript["assembled"]
        if entry["target_id"] == "S01"
    )
    assert section["text"] == "Body [REF:p1]."


def test_ambiguous_citation_rich_target_is_retained_and_audited() -> None:
    original = (
        "# First Section\n\nRepeated [REF:p1].\n\nRepeated [REF:p2]."
    )
    manuscript = assemble_revised_manuscript(
        sections=[
            {
                "section_id": "S01",
                "section_title": "First Section",
                "full_text": original,
                "blocks": [
                    {"block_id": "S01-B001", "prose": "Repeated."}
                ],
            }
        ],
        front_back={},
        revisions={"S01-B001": "Reworded."},
    )
    section = manuscript["assembled"][0]
    assert "Repeated [REF:p1]." in section["text"]
    assert "Repeated [REF:p2]." in section["text"]
    assert "Reworded." not in section["text"]
    assert section["applied_block_revisions"] == []
    assert section["unapplied_block_revisions"] == [
        {
            "block_id": "S01-B001",
            "reason": "citation_rich_target_not_uniquely_aligned",
        }
    ]
    assert manuscript["unapplied_revision_ids"] == ["S01-B001"]
    assert manuscript["revision_applications"]["S01-B001"] == {
        "status": "unapplied",
        "reason": "citation_rich_target_not_uniquely_aligned",
    }


def test_editorial_revision_standalone_markdown_and_resume(
    tmp_path: Path, monkeypatch
) -> None:
    import hashlib

    fake_call, calls = _make_editorial_qwen_mock(
        revised_map={"Old transition": "Smoothed transition [REF:p1]."}
    )
    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call)
    finding = _editorial_finding(
        finding_id="F-1",
        issue_type="ordering_break",
        target_ids=["S01-B001"],
        severity="major",
    )
    inputs = {
        "sections": [
            {
                "section_id": "S01",
                "section_title": "First Section",
                "blocks": [
                    {"block_id": "S01-B001", "prose": "Old transition here [REF:p1]."},
                ],
            }
        ]
    }

    def finding_provider(_stage_input):
        return {"review": {"findings": [finding], "fail_open": True}, "status": "reviewed"}

    def patches_provider(_stage_input):
        return {"proposals": [], "approval_required": False}

    providers = {
        "whole_manuscript_review": finding_provider,
        "bounded_patch_proposals": patches_provider,
        "editorial_revision": make_editorial_revision_qwen_provider(
            qwen_call=fake_call
        ),
    }
    run_kwargs = dict(
        work_dir=tmp_path / "out",
        inputs=inputs,
        stage_providers=providers,
        run_id="markdown-er",
    )
    first = run_staged_article_completion(**run_kwargs)
    md_path = tmp_path / "out" / "STAGED_COMPLETE_REVIEW_EN.md"
    manifest_path = (
        tmp_path / "out" / "STAGED_COMPLETE_REVIEW_EN.sha256.json"
    )
    assert md_path.is_file()
    assert manifest_path.is_file()
    markdown = md_path.read_text(encoding="utf-8")
    assert "## First Section" in markdown
    assert "Smoothed transition [REF:p1]." in markdown
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    assert manifest["sha256"] == expected_digest
    assert manifest["run_id"] == "markdown-er"
    assert manifest["editorial_revision_fingerprint"] == first.stages[
        "editorial_revision"
    ].fingerprint

    calls.clear()
    second = run_staged_article_completion(**run_kwargs, resume=True)
    assert second.stages["editorial_revision"].status == "noop"
    assert calls == []
    assert md_path.read_text(encoding="utf-8") == markdown
    assert (
        json.loads(manifest_path.read_text(encoding="utf-8"))["sha256"]
        == expected_digest
    )


def test_editorial_revision_minor_advisory_no_work_item(
    monkeypatch,
) -> None:
    fake_call, calls = _make_editorial_qwen_mock(
        revised_map={"block one": "block one with term defined [REF:p1]."}
    )
    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call)
    findings = [
        _editorial_finding(
            finding_id="MIN-1",
            issue_type="duplicated_explanation",
            target_ids=["S01-B001", "S01-B002"],
            severity="minor",
            dimension="overlap",
        ),
        _editorial_finding(
            finding_id="ADV-1",
            issue_type="undefined_term",
            target_ids=["S01-B002", "S01-B001"],
            severity="advisory",
            dimension="clarity",
        ),
    ]
    provider = make_editorial_revision_qwen_provider(qwen_call=fake_call)
    output = provider(_editorial_stage_input(findings=findings))
    audit = output["audit"]
    assert audit["work_item_count"] == 0
    assert audit["accepted_count"] == 0
    assert audit["review_findings_kept"] == findings
    assert len(audit["unselected"]) == 2
    assert {entry["reason"] for entry in audit["unselected"]} == {
        "severity_too_low"
    }
    assert {entry["severity"] for entry in audit["unselected"]} == {
        "minor",
        "advisory",
    }
    assert calls == []

    sections = [
        {
            "section_id": "S01",
            "section_title": "One",
            "blocks": [
                {"block_id": "S01-B001", "prose": "a1"},
                {"block_id": "S01-B002", "prose": "a2"},
            ],
        }
    ]
    major_items, _ = plan_editorial_work_items(
        sections=sections,
        front_back={},
        findings=[
            _editorial_finding(
                finding_id="MAJ-1",
                issue_type="undefined_term",
                target_ids=["S01-B002"],
                severity="major",
                dimension="clarity",
            )
        ],
        proposals=[],
    )
    assert len(major_items) == 1
    assert major_items[0].target_block_id == "S01-B002"


def test_editorial_revision_target_collision_proposal_preferred() -> None:
    sections = [
        {
            "section_id": "S01",
            "section_title": "One",
            "blocks": [
                {"block_id": "S01-B001", "prose": "a1"},
                {"block_id": "S01-B002", "prose": "a2"},
            ],
        }
    ]
    dup = _editorial_finding(
        finding_id="DUP-1",
        issue_type="duplicated_explanation",
        target_ids=["S01-B001", "S01-B002"],
        severity="major",
        dimension="overlap",
    )
    term = _editorial_finding(
        finding_id="TERM-1",
        issue_type="undefined_term",
        target_ids=["S01-B002"],
        severity="critical",
        dimension="clarity",
    )
    proposal = {
        "patch_id": "P-1",
        "operation": "rewrite_transition",
        "target": "S01-B002",
        "rationale": "commander transition",
        "approval_required": True,
    }
    items, unresolved, unselected = plan_editorial_work_items_with_provenance(
        sections=sections,
        front_back={},
        findings=[dup, term],
        proposals=[proposal],
    )
    assert unresolved == []
    assert len(items) == 1
    item = items[0]
    assert item.target_block_id == "S01-B002"
    assert item.editorial_kind == "transition"
    assert item.patch_proposal["patch_id"] == "P-1"
    assert item.finding == {}
    assert [
        entry["reason"] for entry in unselected
    ].count("target_collision") == 2
    assert {entry["source_id"] for entry in unselected} == {
        "duplicated_explanation:S01-B001,S01-B002",
        "undefined_term:S01-B002",
    }

    items_no_proposal, _, unselected_no_proposal = (
        plan_editorial_work_items_with_provenance(
            sections=sections,
            front_back={},
            findings=[dup, term],
            proposals=[],
        )
    )
    assert len(items_no_proposal) == 1
    assert items_no_proposal[0].finding["finding_id"] == "TERM-1"
    assert items_no_proposal[0].editorial_kind == (
        "terminology_clarification"
    )
    assert [
        entry["reason"] for entry in unselected_no_proposal
    ].count("target_collision") == 1


def test_editorial_revision_checkpoint_resume(
    tmp_path: Path, monkeypatch
) -> None:
    interrupt = {"active": True}
    calls: list[dict[str, str]] = []

    def fake_call_qwen_chat(agent, messages, **kwargs):
        calls.append({"agent": agent})
        user = json.loads(messages[1]["content"])["stage_input"]
        original = str(user.get("original_text") or "")
        if (
            agent == "StagedEditorialRevisionAuthor"
            and interrupt["active"]
            and "block two" in original
        ):
            raise KeyboardInterrupt
        if agent == "StagedEditorialRevisionAuthor":
            revised = ""
            for marker, replacement in {
                "block one": "block one with term defined [REF:p1].",
                "block two": "block two cross-reference [REF:p2].",
            }.items():
                if marker in original:
                    revised = replacement
                    break
            return {
                "content": json.dumps(
                    {"revised_text": revised, "notes": ""}
                ),
                "_llm_usage": {
                    "agent_name": agent,
                    "model_tier": "c_model",
                    "input_tokens": 3,
                    "output_tokens": 1,
                },
            }
        return {
            "content": json.dumps(
                {
                    "meaning_preserved": True,
                    "scope_preserved": True,
                    "citations_preserved": True,
                    "numbers_conditions_preserved": True,
                    "problem_improved": True,
                    "notes": "ok",
                }
            ),
            "_llm_usage": {
                "agent_name": agent,
                "model_tier": "c2_model",
                "input_tokens": 2,
                "output_tokens": 1,
            },
        }

    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call_qwen_chat)
    findings = [
        _editorial_finding(
            finding_id="TERM-1",
            issue_type="undefined_term",
            target_ids=["S01-B001", "S01-B002"],
            severity="major",
            dimension="clarity",
        ),
        _editorial_finding(
            finding_id="DUP-1",
            issue_type="duplicated_explanation",
            target_ids=["S01-B001", "S01-B002"],
            severity="major",
            dimension="overlap",
        ),
    ]
    stage_input = _editorial_stage_input(
        sections=[
            {"block_id": "S01-B001", "prose": "block one [REF:p1]."},
            {"block_id": "S01-B002", "prose": "block two [REF:p2]."},
        ],
        findings=findings,
    )
    work_dir = tmp_path / "cp"
    provider = make_editorial_revision_qwen_provider(
        qwen_call=fake_call_qwen_chat
    )
    with pytest.raises(KeyboardInterrupt):
        provider(
            stage_input,
            execution_context={"work_dir": str(work_dir), "resume": False},
        )
    checkpoint_path = work_dir / "staged_editorial_revision_checkpoint.json"
    assert checkpoint_path.is_file()
    stored = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert stored["completed"] is False
    assert [entry["work_item_id"] for entry in stored["items"]] == ["ER-001"]
    # ER-001 author + verifier completed; ER-002's author call recorded then
    # interrupted before returning.
    assert calls == [
        {"agent": "StagedEditorialRevisionAuthor"},
        {"agent": "StagedEditorialRevisionVerifier"},
        {"agent": "StagedEditorialRevisionAuthor"},
    ]

    calls.clear()
    interrupt["active"] = False
    resumed = provider(
        stage_input,
        execution_context={"work_dir": str(work_dir), "resume": True},
    )
    assert resumed["audit"]["resumed_from_checkpoint"] is True
    assert calls == [
        {"agent": "StagedEditorialRevisionAuthor"},
        {"agent": "StagedEditorialRevisionVerifier"},
    ]
    stored = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert stored["completed"] is True
    assert [entry["work_item_id"] for entry in stored["items"]] == [
        "ER-001",
        "ER-002",
    ]

    calls.clear()
    fresh = provider(
        stage_input,
        execution_context={
            "work_dir": str(tmp_path / "fresh"),
            "resume": False,
        },
    )
    assert len(calls) == 4

    def audit_without_checkpoint(audit):
        audit = dict(audit)
        audit.pop("checkpoint_fingerprint", None)
        audit.pop("resumed_from_checkpoint", None)
        return audit

    assert audit_without_checkpoint(resumed["audit"]) == audit_without_checkpoint(
        fresh["audit"]
    )
    assert resumed["_usage"] == fresh["_usage"]
    assert resumed["manuscript"] == fresh["manuscript"]

    raw_checkpoint = checkpoint_path.read_text(encoding="utf-8").casefold()
    assert "api_key" not in raw_checkpoint
    assert "authorization" not in raw_checkpoint
    assert "secret" not in raw_checkpoint


def test_editorial_revision_checkpoint_mismatch_not_reused(
    tmp_path: Path, monkeypatch
) -> None:
    fake_call, calls = _make_editorial_qwen_mock(
        revised_map={
            "block one": "block one with term defined [REF:p1].",
            "block two": "block two cross-reference [REF:p2].",
        }
    )
    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call)
    findings = [
        _editorial_finding(
            finding_id="TERM-1",
            issue_type="undefined_term",
            target_ids=["S01-B001", "S01-B002"],
            severity="major",
            dimension="clarity",
        ),
        _editorial_finding(
            finding_id="DUP-1",
            issue_type="duplicated_explanation",
            target_ids=["S01-B001", "S01-B002"],
            severity="major",
            dimension="overlap",
        ),
    ]
    stage_input = _editorial_stage_input(
        sections=[
            {"block_id": "S01-B001", "prose": "block one [REF:p1]."},
            {"block_id": "S01-B002", "prose": "block two [REF:p2]."},
        ],
        findings=findings,
    )
    work_dir = tmp_path / "cp"
    provider = make_editorial_revision_qwen_provider(qwen_call=fake_call)
    provider(
        stage_input,
        execution_context={"work_dir": str(work_dir), "resume": False},
    )
    checkpoint_path = work_dir / "staged_editorial_revision_checkpoint.json"
    stored = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    original_fingerprint = stored["fingerprint"]
    stored["fingerprint"] = "mismatched"
    checkpoint_path.write_text(json.dumps(stored), encoding="utf-8")

    calls.clear()
    resumed = provider(
        stage_input,
        execution_context={"work_dir": str(work_dir), "resume": True},
    )
    assert resumed["audit"]["resumed_from_checkpoint"] is False
    assert len(calls) == 4
    stored = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert stored["fingerprint"] == original_fingerprint


def test_introduction_prompt_requires_real_ref_markers() -> None:
    prompts_dir = Path(__file__).resolve().parents[1] / "prompts"
    introduction = (
        prompts_dir / "Staged Introduction Author.txt"
    ).read_text(encoding="utf-8")
    assert "exact [REF:identity] token" in introduction
    assert "[REF:s2:a]" in introduction
    assert "Cite ONLY identities listed in" in introduction
    assert "background_sources" in introduction
    assert "never invent, alter, or substitute marker identities" in introduction
    assert "placeholder markers" in introduction


def test_front_matter_prompts_forbid_unsupported_absolute_wording() -> None:
    prompts_dir = Path(__file__).resolve().parents[1] / "prompts"
    for name in (
        "Staged Conclusion Author.txt",
        "Staged Introduction Author.txt",
        "Staged Abstract Author.txt",
    ):
        text = (prompts_dir / name).read_text(encoding="utf-8")
        assert "Calibrated language" in text, name
        assert "guaranteed" in text, name
        assert "machine-precision" in text, name
        assert "superior" in text, name
        assert "handed-off manuscript explicitly supports" in text, name
        assert "is consistent with" in text, name


def test_approval_required_survives_resume_noop(tmp_path: Path) -> None:
    def patches_provider(_stage_input):
        return {"proposals": [], "approval_required": True}

    providers = {"bounded_patch_proposals": patches_provider}
    run_kwargs = dict(
        work_dir=tmp_path / "out",
        inputs={"section_order": ["S01"]},
        stage_providers=providers,
        run_id="approval-resume",
    )
    first = run_staged_article_completion(**run_kwargs)
    stage = first.stages["bounded_patch_proposals"]
    assert stage.approval_required is True
    assert stage.status == "awaiting_approval"
    assert first.awaiting_approval_stages == ["bounded_patch_proposals"]
    assert first.status == "awaiting_approval"

    second = run_staged_article_completion(**run_kwargs, resume=True)
    stage = second.stages["bounded_patch_proposals"]
    assert stage.status == "noop"
    assert stage.approval_required is True
    assert second.awaiting_approval_stages == ["bounded_patch_proposals"]
    assert second.status == "awaiting_approval"
    summary = cli._summary(second)
    assert summary["approval_required_stages"] == [
        "bounded_patch_proposals"
    ]

    direct = StagedArticleCompletionState(
        run_id="direct",
        run_fingerprint="fp",
        stage_order=list(STAGE_ORDER),
        status="completed",
        stages={
            "bounded_patch_proposals": StagedStageState(
                stage="bounded_patch_proposals",
                status="noop",
                approval_required=True,
            )
        },
    )
    assert direct.awaiting_approval_stages == ["bounded_patch_proposals"]


def test_multi_reviewer_roles_run_concurrently_and_aggregate_deterministically() -> None:
    import threading
    import time

    lock = threading.Lock()
    active = {"value": 0}
    max_active = {"value": 0}
    calls: list[dict[str, Any]] = []

    def fake_call(agent: str, messages: list[dict], **kwargs):
        payload = json.loads(messages[1]["content"])
        stage_input = payload.get("stage_input") or payload
        reviewer_id = str(stage_input.get("reviewer_id") or "?")
        with lock:
            active["value"] += 1
            max_active["value"] = max(
                max_active["value"], active["value"]
            )
            calls.append({"agent": agent, "reviewer_id": reviewer_id})
        try:
            time.sleep(0.01)  # widen the overlap window for the assertion
            return {
                "content": json.dumps(
                    {
                        "findings": [
                            {
                                "dimension": "clarity",
                                "severity": "major",
                                "statement": f"finding from {reviewer_id}",
                                "issue_type": "clarity_note",
                                "target_ids": ["S01"],
                            }
                        ]
                    }
                ),
                "_llm_usage": {"input_tokens": 4, "output_tokens": 2},
            }
        finally:
            with lock:
                active["value"] -= 1

    provider = make_multi_reviewer_qwen_provider(
        reviewers=[
            {"reviewer_id": f"r{index}", "role": "clarity"}
            for index in range(1, 4)
        ],
        qwen_call=fake_call,
        workers=3,
    )
    output = provider({"stage": "whole_manuscript_review", "inputs": {}})

    assert max_active["value"] >= 2
    assert [call["reviewer_id"] for call in calls] == ["r1", "r2", "r3"]
    assert output["_usage"]["call_count"] == 3
    assert output["_usage"]["workers"] == 3
    assert [role["reviewer_id"] for role in output["reviewers"]] == [
        "r1",
        "r2",
        "r3",
    ]
    assert output["review"]["findings"]


def test_editorial_same_section_serial_cross_section_parallel(
    tmp_path: Path, monkeypatch
) -> None:
    import threading
    import time

    lock = threading.Lock()
    active_by_section: dict[str, int] = {}
    max_by_section: dict[str, int] = {}
    global_active = {"value": 0}
    global_max = {"value": 0}
    author_blocks: list[str] = []

    def fake_call(agent: str, messages: list[dict], **kwargs):
        if agent == "StagedEditorialRevisionVerifier":
            return {
                "content": json.dumps(
                    {
                        "meaning_preserved": True,
                        "scope_preserved": True,
                        "citations_preserved": True,
                        "numbers_conditions_preserved": True,
                        "problem_improved": True,
                        "notes": "ok",
                    }
                ),
                "_llm_usage": {"input_tokens": 2, "output_tokens": 1},
            }
        payload = json.loads(messages[1]["content"])
        stage_input = payload.get("stage_input") or payload
        block_id = str(stage_input.get("target_block_id") or "?")
        section = str(block_id).split("-", 1)[0]
        with lock:
            active_by_section[section] = (
                active_by_section.get(section, 0) + 1
            )
            max_by_section[section] = max(
                max_by_section.get(section, 0),
                active_by_section[section],
            )
            global_active["value"] += 1
            global_max["value"] = max(
                global_max["value"], global_active["value"]
            )
            author_blocks.append(block_id)
        try:
            time.sleep(0.01)  # widen the overlap window for the assertion
            original = str(stage_input.get("original_text") or "")
            return {
                "content": json.dumps(
                    {
                        "revised_text": original + " smoothed.",
                        "notes": "",
                    }
                ),
                "_llm_usage": {"input_tokens": 3, "output_tokens": 1},
            }
        finally:
            with lock:
                active_by_section[section] -= 1
                global_active["value"] -= 1

    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_call)
    provider = make_editorial_revision_qwen_provider(
        qwen_call=fake_call,
        workers=2,
    )
    sections = [
        {
            "section_id": "S01",
            "section_title": "First",
            "chapter_thesis": "thesis",
            "reader_takeaway": "takeaway",
            "blocks": [
                {
                    "block_id": "S01-B001",
                    "prose": "Old transition here [REF:p1].",
                },
                {
                    "block_id": "S01-B002",
                    "prose": "Middle block content [REF:p2].",
                },
            ],
        },
        {
            "section_id": "S02",
            "section_title": "Second",
            "chapter_thesis": "thesis2",
            "reader_takeaway": "takeaway2",
            "blocks": [
                {
                    "block_id": "S02-B001",
                    "prose": "Second transition [REF:p3].",
                },
                {
                    "block_id": "S02-B002",
                    "prose": "Second middle [REF:p4].",
                },
            ],
        },
    ]
    findings = [
        _editorial_finding(
            finding_id=f"F-{index}",
            issue_type="ordering_break",
            target_ids=[block],
            severity="major",
        )
        for index, block in enumerate(
            [
                "S01-B001",
                "S01-B002",
                "S02-B001",
                "S02-B002",
            ],
            start=1,
        )
    ]
    stage_input = {
        "stage": "editorial_revision",
        "inputs": {"sections": sections},
        "previous_artifacts": {
            "whole_manuscript_review": {
                "fingerprint": "review-fp",
                "payload": {
                    "review": {
                        "findings": [
                            dict(finding) for finding in findings
                        ],
                        "fail_open": True,
                    },
                    "status": "reviewed",
                },
            },
            "bounded_patch_proposals": {
                "fingerprint": "patches-fp",
                "payload": {
                    "proposals": [],
                    "approval_required": False,
                },
            },
            "conclusion": {
                "fingerprint": "c-fp",
                "payload": {"draft": {"text": "conclusion"}},
            },
            "introduction": {
                "fingerprint": "i-fp",
                "payload": {"draft": {"text": "introduction"}},
            },
            "abstract": {
                "fingerprint": "a-fp",
                "payload": {"draft": {"text": "abstract"}},
            },
        },
    }
    output = provider(stage_input)

    assert global_max["value"] >= 2
    assert all(
        max_by_section.get(section, 0) == 1
        for section in ("S01", "S02")
    )
    assert output["audit"]["work_item_count"] == 4
    assert output["audit"]["accepted_count"] == 4
    assert [record["target_block_id"] for record in output["audit"]["records"]] == [
        "S01-B001",
        "S01-B002",
        "S02-B001",
        "S02-B002",
    ]
    assert len(author_blocks) == 4
