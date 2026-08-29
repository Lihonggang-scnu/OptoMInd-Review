"""Offline acceptance tests for the read-only global manuscript commander.

The suite proves full chapter text delivery to every role, local/authoritative
IDs, cross-section overlap, read-only behavior, retrieval non-invocation,
tolerant parsing with one bounded repair call, unknown-ID removal, honest
commander failure, resume idempotency, fingerprint refusal, and citation/claim
metadata collection. No network or API calls are made.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Callable

import pytest

import llm.qwen_chat_client as qwen_client_module

from optomind_research.runtime.global_manuscript_commander import (
    DEFAULT_PROMPTS_DIR,
    DEFAULT_MODEL_TIER,
    DeterministicRoleProvider,
    ManifestError,
    QwenRoleProvider,
    ResumeFingerprintMismatch,
    _role_context_view,
    apply_m4_patch_set,
    audit_m4_claim_evidence_ledger,
    build_canonical_context,
    build_m4_snapshot,
    compute_fingerprint,
    load_manifest,
    parse_role_output,
    run_global_manuscript_commander,
    sanitize_role_result,
    _split_paragraphs,
    validate_m4_patch_set,
    _word_count,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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
    }


def _default_evidence(section_id: str) -> list[dict[str, Any]]:
    return [
        {
            "claim_id": f"{section_id}-CL01",
            "chunk_id": f"chunk-{section_id}-1",
            "paper_id": f"paper_{section_id}",
            "source_title": f"Paper title {section_id}",
            "support_relation": "direct",
            "evidence_level": "fulltext",
            "source_kind": "s2_body",
            "scope_fit": "in_domain",
            "retrieval_role": "evidence_candidate",
            "provenance_type": "real_paper_id",
        }
    ]


def _write_section(
    root: Path,
    section_id: str,
    title: str,
    text: str,
    *,
    claims: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    reviewer_comments: list[dict[str, Any]] | None = None,
    visual_gap_plan: list[dict[str, Any]] | None = None,
    expected_visual_arguments: list[str] | None = None,
    workplan: list[dict[str, Any]] | None = None,
    manuscript_context_overrides: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    section_dir = root / section_id
    section_dir.mkdir(parents=True, exist_ok=True)
    draft_path = section_dir / "SECTION_DRAFT_EN.md"
    packet_path = section_dir / "input_packet.json"
    draft_path.write_text(text, encoding="utf-8")
    manuscript_context = {
        "source_section_title": title,
        "research_context": {
            "user_question": f"Question {section_id}",
            "scope_definition": f"Scope {section_id}",
        },
        "global_review_thesis": "Global thesis",
        "global_narrative_strategy": "Global strategy",
        "current_section_boundary_contract": {
            "section_id": section_id,
            "title": title,
            "handoff_from_previous": f"handoff-from-{section_id}",
            "handoff_to_next": f"handoff-to-{section_id}",
        },
        "full_section_workplan": workplan or [],
        "sibling_section_responsibilities": [],
        "reviewer_comments_retained": reviewer_comments or [],
        "excluded_unready_claim_ids": [],
        "write_gate": {"allowed_to_write": True},
        "evidence_provenance": {},
    }
    if manuscript_context_overrides:
        manuscript_context.update(manuscript_context_overrides)
    packet = {
        "section_id": section_id,
        "section_contract": {
            "section_id": section_id,
            "title": title,
            "argument_role": {"statement": f"Argument role for {section_id}"},
            "section_purpose": f"Purpose for {section_id}",
            "unique_contribution": f"Unique contribution for {section_id}",
            "must_cover": [f"must-cover-{section_id}"],
            "must_not_cover": [f"must-not-cover-{section_id}"],
            "assigned_user_axes": [f"axis-{section_id}"],
            "handoff_from_previous": f"handoff-from-{section_id}",
            "handoff_to_next": f"handoff-to-{section_id}",
            "central_thesis": f"Thesis for {section_id}",
            "paragraph_functions": [
                {
                    "paragraph_index": 1,
                    "title": f"Opening paragraph of {section_id}",
                    "purpose": f"Purpose of paragraph 1 in {section_id}",
                    "claim_ids": [f"{section_id}-CL01"],
                    "transition_logic": f"Transition for {section_id}",
                }
            ],
            "expected_visual_arguments": expected_visual_arguments or [],
            "open_questions": [],
            "word_budget": 1200,
        },
        "claims": claims if claims is not None else [_default_claim(section_id)],
        "evidence_packets": (
            evidence
            if evidence is not None
            else _default_evidence(section_id)
        ),
        "contradictions": [],
        "open_questions": [f"Open question {section_id}"],
        "transition_contract": {},
        "uncited_load_bearing_claim_ids": [],
        "visual_evidence": [],
        "visual_gap_plan": visual_gap_plan or [],
        "manuscript_context": manuscript_context,
        "literature_coverage": {
            "sources": [],
            "paper_ids": [],
            "evidence_chunk_ids": [],
        },
    }
    packet_path.write_text(
        json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return draft_path, packet_path


def _write_manifest(
    root: Path, sections: list[dict[str, Any]]
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": (
                    "optomind.global_manuscript_commander.manifest.v1"
                ),
                "sections": sections,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _write_ledger(
    root: Path,
    section_id: str,
    records: list[dict[str, Any]],
) -> Path:
    path = root / section_id / "EXPLANATORY_CITATION_LEDGER.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "chapter_asset_enhancer.v1",
                "section_id": section_id,
                "records": records,
            }
        ),
        encoding="utf-8",
    )
    return path


def _three_sections(root: Path) -> tuple[Path, list[dict[str, Any]], dict[str, str]]:
    sections: list[dict[str, Any]] = []
    markers: dict[str, str] = {}
    for section_id, title in (
        ("S01", "First Chapter"),
        ("S02", "Second Chapter"),
        ("S03", "Third Chapter"),
    ):
        marker = f"UNIQUE_FULL_TEXT_MARKER_{section_id}_" + ("x" * 160)
        draft = (
            f"# {title}\n\n"
            f"Opening paragraph of {section_id} with [REF:paper_{section_id}].\n\n"
            f"{marker}\n"
        )
        draft_path, packet_path = _write_section(
            root, section_id, title, draft
        )
        sections.append(
            {
                "section_id": section_id,
                "english_draft_path": str(draft_path),
                "input_packet_path": str(packet_path),
            }
        )
        markers[section_id] = marker
    return _write_manifest(root, sections), sections, markers


def _source_hashes(
    manifest: Path, sections: list[dict[str, Any]]
) -> dict[str, str]:
    paths = [manifest]
    for section in sections:
        paths.append(Path(section["english_draft_path"]))
        paths.append(Path(section["input_packet_path"]))
    return {str(path): _sha256(path) for path in paths}


class Provider:
    """Configurable offline provider that records every call."""

    def __init__(
        self,
        responders: dict[str, Any] | None = None,
        delegate: Any | None = None,
    ) -> None:
        self.responders = responders or {}
        self.delegate = delegate or DeterministicRoleProvider()
        self.calls: list[dict[str, Any]] = []

    def __call__(self, role: str, payload: dict[str, Any]) -> Any:
        self.calls.append({"role": role, "payload": payload})
        responder = self.responders.get(role)
        if responder is not None:
            return responder(role, payload)
        return self.delegate(role, payload)


def test_full_chapter_text_reaches_every_role_and_final_commander(
    tmp_path: Path,
) -> None:
    root = tmp_path / "assets"
    manifest, sections, markers = _three_sections(root)
    provider = Provider()
    summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=tmp_path / "out",
        role_provider=provider,
    )
    assert summary["status"] == "completed"
    assert len(provider.calls) == 5
    for call in provider.calls:
        view = call["payload"]["canonical_context"]
        assert view["section_count"] == 3
        for section in view["sections"]:
            section_id = section["section_id"]
            assert markers[section_id] in section["draft_text"]
            assert section["draft_text"].rstrip().endswith(
                markers[section_id]
            )


def test_ledgers_local_ids_overlap_and_metadata(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    sections: list[dict[str, Any]] = []
    for section_id, title in (("S01", "First"), ("S02", "Second")):
        draft = (
            f"# {title}\n\n"
            f"Paragraph one of {section_id} [REF:shared_paper_1].\n\n"
            f"Paragraph two of {section_id} [REF:paper_{section_id}].\n"
        )
        claims = [
            _default_claim(section_id),
            {
                "claim_id": f"{section_id}-CL02",
                "role": "contextual",
                "claim_state": "rejected",
                "evidence_binding_status": "partial",
                "writing_permission": "background_only",
                "parent_claim_id": f"{section_id}-CL01",
                "evidence_strength": "weak",
                "statement": f"{section_id} secondary claim",
            },
        ]
        evidence = [
            {
                "claim_id": f"{section_id}-CL01",
                "chunk_id": f"chunk-shared-{section_id}",
                "paper_id": "shared_paper_1",
                "source_title": "Shared Review Paper",
                "support_relation": "direct",
                "evidence_level": "fulltext",
                "source_kind": "s2_body",
                "scope_fit": "in_domain",
                "retrieval_role": "evidence_candidate",
                "provenance_type": "real_paper_id",
            }
        ]
        draft_path, packet_path = _write_section(
            root,
            section_id,
            title,
            draft,
            claims=claims,
            evidence=evidence,
        )
        sections.append(
            {
                "section_id": section_id,
                "english_draft_path": str(draft_path),
                "input_packet_path": str(packet_path),
            }
        )
    manifest = _write_manifest(root, sections)
    output = tmp_path / "out"
    summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=Provider(),
    )
    assert summary["status"] == "completed"
    canonical = json.loads(
        (output / "canonical_context.json").read_text(encoding="utf-8")
    )
    first = next(
        section
        for section in canonical["sections"]
        if section["section_id"] == "S01"
    )
    assert first["paragraphs"][0]["paragraph_id"] == "P01"
    assert first["paragraphs"][0]["canonical_id"] == "S01-P01"
    assert first["paragraphs"][0]["contract_claim_ids"] == ["S01-CL01"]
    assert canonical["papers"]["shared_paper_1"]["primary_title"] == (
        "Shared Review Paper"
    )
    assert canonical["papers"]["shared_paper_1"]["sections"] == ["S01", "S02"]
    overlap = canonical["cross_section_overlap"]["shared_paper_1"]
    assert overlap["section_pairs"] == [["S01", "S02"]]
    assert canonical["ref_identity_map"]["shared_paper_1"]["known"] is True
    assert canonical["ref_identity_map"]["shared_paper_1"]["title"] == (
        "Shared Review Paper"
    )
    claims = {claim["claim_id"]: claim for claim in first["claims"]}
    assert claims["S01-CL01"]["role"] == "load_bearing"
    assert claims["S01-CL01"]["readiness"] == "ready_for_write"
    assert claims["S01-CL02"]["readiness"] == "not_ready"
    assert summary["cross_section_overlap_count"] == 1


def test_unknown_ids_dropped_with_audit_notes(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)

    def structure_responder(
        role: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "diagnosis": {"strengths": ["ok"], "risks": []},
            "proposed_section_order": [
                {"section_id": "S99", "reason": "bogus"},
                {"section_id": "S01", "reason": "ok"},
            ],
            "section_decisions": [
                {"section_id": "S01", "decision": "retain", "rationale": "ok"}
            ],
            "cross_section_conflicts": [],
            "missing_axes": [],
            "structure_gaps": [],
            "paragraph_references": [
                {
                    "section_id": "S01",
                    "paragraph_id": "S01-P99",
                    "note": "bogus",
                },
                {
                    "section_id": "S01",
                    "paragraph_id": "P01",
                    "note": "local form normalized",
                },
            ],
            "repeated_paper_roles": [
                {
                    "paper_id": "ghost_paper",
                    "title": "Ghost",
                    "sections": ["S01"],
                    "roles": [],
                    "recommendation": "",
                }
            ],
            "retained_advisory_issues": [],
        }

    def critic_responder(
        role: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "citation_audit": [
                {
                    "section_id": "S01",
                    "paragraph_id": "S01-P01",
                    "ref_marker": "bogus_ref",
                    "paper_id": "",
                    "title": "",
                    "status": "unknown_identity",
                    "note": "bogus",
                },
                {
                    "section_id": "S01",
                    "paragraph_id": "S01-P01",
                    "ref_marker": "paper_S01",
                    "paper_id": "paper_S01",
                    "title": "Paper title S01",
                    "status": "verified",
                    "note": "ok",
                },
            ],
            "attribution_issues": [],
            "source_concentration": [],
            "evidence_outline_discipline": [],
            "retrieval_gap_proposals": [
                {
                    "gap_type": "retrieval_now",
                    "section_id": "S01",
                    "reason": "bogus type",
                },
                {
                    "gap_type": "claim_evidence_gap",
                    "section_id": "S01",
                    "claim_id": "S01-CL01",
                    "reason": "valid",
                    "proposed_retrieval_scope": "bounded future scope",
                },
            ],
            "visual_evidence_notes": [],
            "retained_advisory_issues": [],
        }

    provider = Provider(
        responders={
            "structure_strategist": structure_responder,
            "evidence_attribution_critic": critic_responder,
        }
    )
    output = tmp_path / "out"
    summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=provider,
    )
    assert summary["status"] == "completed"
    reviews = json.loads(
        (output / "role_reviews.json").read_text(encoding="utf-8")
    )
    structure = reviews["roles"]["structure_strategist"]["result"]
    assert [item["section_id"] for item in structure["proposed_section_order"]] == [
        "S01"
    ]
    assert structure["paragraph_references"] == [
        {
            "section_id": "S01",
            "paragraph_id": "S01-P01",
            "note": "local form normalized",
            "finding_id": "",
        }
    ]
    assert structure["repeated_paper_roles"] == []
    structure_issues = reviews["roles"]["structure_strategist"][
        "validation_issues"
    ]
    assert any("unknown section_id 'S99'" in issue for issue in structure_issues)
    assert any(
        "unknown paragraph_id 'S01-P99'" in issue for issue in structure_issues
    )
    critic = reviews["roles"]["evidence_attribution_critic"]["result"]
    assert [item["ref_marker"] for item in critic["citation_audit"]] == [
        "paper_S01"
    ]
    assert [
        item["gap_type"] for item in critic["retrieval_gap_proposals"]
    ] == ["claim_evidence_gap"]
    critic_issues = reviews["roles"]["evidence_attribution_critic"][
        "validation_issues"
    ]
    assert any(
        "unsupported gap_type 'retrieval_now'" in issue
        for issue in critic_issues
    )
    assert any(
        "unknown REF identity 'bogus_ref'" in issue for issue in critic_issues
    )


def test_fenced_json_and_bounded_single_repair(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)

    def structure_responder(
        role: str, payload: dict[str, Any]
    ) -> str:
        section_ids = [
            section["section_id"]
            for section in payload["canonical_context"]["sections"]
        ]
        content = {
            "diagnosis": {"strengths": [], "risks": []},
            "structure_candidates": [
                {
                    "story_shape": "manifest_order",
                    "narrative_backbone": "Keep manifest order.",
                    "section_order": list(section_ids),
                    "reader_path": "Follow manifest order.",
                    "rationale": "fenced dry output",
                    "risks": [],
                },
                {
                    "story_shape": "reversed_order",
                    "narrative_backbone": "Reverse reading path.",
                    "section_order": list(reversed(section_ids)),
                    "reader_path": "Backtrack from synthesis.",
                    "rationale": "alternative shape",
                    "risks": [],
                },
            ],
            "proposed_section_order": [
                {
                    "section_id": section["section_id"],
                    "reason": "fenced dry output",
                }
                for section in payload["canonical_context"]["sections"]
            ],
            "section_decisions": [
                {
                    "section_id": section["section_id"],
                    "decision": "retain",
                    "rationale": "fenced dry output",
                }
                for section in payload["canonical_context"]["sections"]
            ],
        }
        return "```json\n" + json.dumps(content) + "\n```"

    def editor_responder(role: str, payload: dict[str, Any]) -> str:
        if payload.get("repair_request"):
            return json.dumps(
                {
                    "synthesis_findings": [],
                    "narrative_progression": [],
                    "overlap_recommendations": [],
                    "repeated_paper_roles": [],
                    "source_concentration": [],
                    "evidence_outline_discipline": [],
                    "unresolved_issues": [],
                    "retained_advisory_issues": [],
                }
            )
        return "this is definitely not json"

    provider = Provider(
        responders={
            "structure_strategist": structure_responder,
            "scientific_synthesis_editor": editor_responder,
        }
    )
    output = tmp_path / "out"
    summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=provider,
    )
    assert summary["status"] == "completed"
    reviews = json.loads(
        (output / "role_reviews.json").read_text(encoding="utf-8")
    )
    structure = reviews["roles"]["structure_strategist"]
    editor = reviews["roles"]["scientific_synthesis_editor"]
    assert structure["repair_used"] is False
    assert structure["status"] == "completed"
    assert editor["repair_used"] is True
    assert editor["status"] == "completed"
    assert any("bounded repair" in issue for issue in editor["validation_issues"])
    assert summary["repair_calls"]["scientific_synthesis_editor"] is True
    assert summary["repair_calls"]["structure_strategist"] is False


def test_partial_role_output_tolerated(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)

    def editor_responder(role: str, payload: dict[str, Any]) -> str:
        return "not json at all"

    provider = Provider(
        responders={"scientific_synthesis_editor": editor_responder}
    )
    output = tmp_path / "out"
    summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=provider,
    )
    assert summary["status"] == "completed"
    reviews = json.loads(
        (output / "role_reviews.json").read_text(encoding="utf-8")
    )
    editor = reviews["roles"]["scientific_synthesis_editor"]
    critic = reviews["roles"]["evidence_attribution_critic"]
    assert editor["status"] == "partial"
    assert editor["result"] == {}
    assert critic["status"] == "completed"
    assert critic["result"]["retrieval_gap_proposals"] == []


def test_no_source_modification_and_no_retrieval(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)

    def commander_responder(
        role: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        base = DeterministicRoleProvider()(role, payload)
        base["retrieval_gap_proposals"] = [
            {
                "gap_type": "visual_evidence_gap",
                "section_id": "S01",
                "reason": "planned visual lacks evidence",
                "proposed_retrieval_scope": (
                    "future authorized visual evidence search"
                ),
            }
        ]
        base["read_only_declaration"] = {
            "chapter_text_changed": True,
            "retrieval_launched": True,
            "note": "model lied",
        }
        return base

    provider = Provider(
        responders={"commander_synthesis": commander_responder}
    )
    output = tmp_path / "out"
    before = _source_hashes(manifest, sections)
    summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=provider,
    )
    after = _source_hashes(manifest, sections)
    assert before == after
    for call in provider.calls:
        payload = call["payload"]
        for key in (
            "launch_retrieval",
            "execute_retrieval",
            "enqueue_retrieval",
            "retrieval_action",
        ):
            assert key not in payload
    work_order = json.loads(
        (output / "global_commander_work_order.json").read_text(
            encoding="utf-8"
        )
    )
    assert work_order["read_only_declaration"]["chapter_text_changed"] is False
    assert work_order["read_only_declaration"]["retrieval_launched"] is False
    assert summary["retrieval_gap_proposal_count"] == 1
    assert summary["read_only_declaration"]["retrieval_launched"] is False
    reviews = json.loads(
        (output / "role_reviews.json").read_text(encoding="utf-8")
    )
    commander_issues = reviews["roles"]["commander_synthesis"][
        "validation_issues"
    ]
    assert any(
        "model claimed retrieval launched" in issue
        for issue in commander_issues
    )


def test_failed_final_commander_is_honest(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)
    provider = Provider(
        responders={
            "commander_synthesis": lambda role, payload: "garbage output"
        }
    )
    output = tmp_path / "out"
    before = _source_hashes(manifest, sections)
    summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=provider,
    )
    after = _source_hashes(manifest, sections)
    assert before == after
    assert summary["status"] == "failed"
    assert "commander_synthesis" in summary["error"]
    work_order = json.loads(
        (output / "global_commander_work_order.json").read_text(
            encoding="utf-8"
        )
    )
    assert work_order["status"] == "failed"
    assert work_order["read_only_declaration"]["chapter_text_changed"] is False
    assert work_order["read_only_declaration"]["retrieval_launched"] is False
    reviews = json.loads(
        (output / "role_reviews.json").read_text(encoding="utf-8")
    )
    assert reviews["roles"]["commander_synthesis"]["status"] == "failed"
    assert reviews["roles"]["structure_strategist"]["status"] == "completed"
    state = json.loads(
        (output / "run_state.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "failed"
    assert "commander_synthesis" in state["error"]
    assert summary["error"] and "commander_synthesis" in summary["error"]
    for key in (
        "canonical_context",
        "role_reviews",
        "global_commander_work_order",
        "run_state",
        "summary",
    ):
        assert Path(summary["outputs"][key]).is_file()
    assert summary["read_only_declaration"]["chapter_text_changed"] is False
    assert summary["read_only_declaration"]["retrieval_launched"] is False


def test_resume_skips_completed_calls(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)
    output = tmp_path / "out"
    first_provider = Provider()
    first_summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=first_provider,
    )
    assert first_summary["status"] == "completed"
    assert len(first_provider.calls) == 5

    def boom(role: str, payload: dict[str, Any]) -> Any:
        raise AssertionError("provider must not be called on resume")

    second_summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        resume=True,
        role_provider=boom,
    )
    assert second_summary["status"] == "completed"
    assert second_summary["resumed"] is True
    reviews = json.loads(
        (output / "role_reviews.json").read_text(encoding="utf-8")
    )
    assert all(
        record["resume_skipped"] is True
        for record in reviews["roles"].values()
    )
    state = json.loads(
        (output / "run_state.json").read_text(encoding="utf-8")
    )
    assert state["resumed"] is True
    assert all(
        stage["resume_skipped"] is True
        for stage in state["stages"].values()
    )


def test_resume_refuses_fingerprint_change(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)
    output = tmp_path / "out"
    first_summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=Provider(),
    )
    assert first_summary["status"] == "completed"
    draft = Path(sections[0]["english_draft_path"])
    draft.write_text(
        draft.read_text(encoding="utf-8") + "\nchanged after run\n",
        encoding="utf-8",
    )

    def boom(role: str, payload: dict[str, Any]) -> Any:
        raise AssertionError("provider must not be called")

    with pytest.raises(ResumeFingerprintMismatch, match="fingerprint"):
        run_global_manuscript_commander(
            manifest_path=manifest,
            output_dir=output,
            resume=True,
            role_provider=boom,
        )


def test_dry_mode_without_provider_produces_package(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)
    output = tmp_path / "out"
    summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        model_tier=DEFAULT_MODEL_TIER,
    )
    assert summary["status"] == "completed"
    assert summary["mode"] == "dry"
    for name in (
        "canonical_context.json",
        "role_reviews.json",
        "global_commander_work_order.json",
        "run_state.json",
        "summary.json",
    ):
        assert (output / name).is_file()
    work_order = json.loads(
        (output / "global_commander_work_order.json").read_text(
            encoding="utf-8"
        )
    )
    for key in (
        "manuscript_diagnosis",
        "proposed_section_order",
        "section_decisions",
        "cross_section_conflicts",
        "missing_axes",
        "structure_gaps",
        "repeated_paper_role_audit",
        "visual_work_orders",
        "retrieval_gap_proposals",
        "affected_section_ids",
        "next_execution_stages",
        "retained_advisory_issues",
        "read_only_declaration",
    ):
        assert key in work_order
    assert work_order["read_only_declaration"]["retrieval_launched"] is False


def test_prompt_files_are_part_of_fingerprint(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)
    prompts_copy = tmp_path / "prompts_copy"
    prompts_copy.mkdir(parents=True, exist_ok=True)
    for name in (
        "Global Manuscript Structure Strategist.txt",
        "Global Manuscript Scientific Editor.txt",
        "Global Manuscript Coverage Auditor.txt",
        "Global Manuscript Gap Value Critic.txt",
        "Global Manuscript Commander.txt",
    ):
        shutil.copy2(DEFAULT_PROMPTS_DIR / name, prompts_copy / name)
    structure_prompt = prompts_copy / "Global Manuscript Structure Strategist.txt"
    structure_prompt.write_text(
        structure_prompt.read_text(encoding="utf-8") + "\n# modified\n",
        encoding="utf-8",
    )
    output = tmp_path / "out"
    first_summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=Provider(),
        prompts_dir=prompts_copy,
    )
    assert first_summary["status"] == "completed"

    def boom(role: str, payload: dict[str, Any]) -> Any:
        raise AssertionError("provider must not be called")

    with pytest.raises(ResumeFingerprintMismatch, match="fingerprint"):
        run_global_manuscript_commander(
            manifest_path=manifest,
            output_dir=output,
            resume=True,
            role_provider=boom,
            prompts_dir=DEFAULT_PROMPTS_DIR,
        )


def test_parse_role_output_tolerates_fenced_and_trailing_prose() -> None:
    raw = 'prefix text\n```json\n{"a": 1}\n```\nsuffix text'
    parsed, notes = parse_role_output(raw)
    assert parsed == {"a": 1}
    assert notes == []
    with pytest.raises(ValueError):
        parse_role_output("no json here")


def test_manifest_requires_all_section_fields(tmp_path: Path) -> None:
    manifest = tmp_path / "bad_manifest.json"
    manifest.write_text(
        json.dumps({"sections": [{"section_id": "S01"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="english_draft_path"):
        load_manifest(manifest)


def test_role_context_view_carries_exact_evidence_spans_and_limitations(
    tmp_path: Path,
) -> None:
    root = tmp_path / "assets"
    quote = (
        "Verbatim exact span with numerical result 42.7 and a [39] citation "
        "marker inside the quote."
    )
    limitations = [
        "Limited to stiff multi-physics PDE systems.",
        "Single benchmark domain; do not generalize beyond it.",
    ]
    evidence = [
        {
            "claim_id": "S01-CL01",
            "chunk_id": "chunk-1",
            "paper_id": "paper_S01",
            "source_title": "Paper title S01",
            "exact_spans": [quote],
            "limitations": limitations,
            "support_relation": "direct",
            "evidence_level": "fulltext",
            "source_kind": "s2_body",
            "scope_fit": "in_domain",
            "retrieval_role": "evidence_candidate",
            "provenance_type": "real_paper_id",
        }
    ]
    claim = {
        "claim_id": "S01-CL01",
        "role": "load_bearing",
        "claim_state": "ready_for_write",
        "evidence_binding_status": "direct",
        "writing_permission": "factual_support",
        "parent_claim_id": "S01-CL00",
        "evidence_strength": "strong",
        "statement": "Full approved claim statement preserved verbatim.",
        "claim_scope_contract": {
            "claim_id": "S01-CL01",
            "generality_ceiling": "bounded_benchmark",
            "applicability": {
                "subject_or_system": "PINN multi-physics regimes",
                "conditions": ["stiff PDE systems"],
            },
            "prohibited_extrapolations": [
                "Do not extrapolate to all regimes."
            ],
        },
    }
    marker = "ROLE_VIEW_MARKER_S01_" + ("y" * 120)
    draft = (
        f"# First\n\nIntro paragraph [REF:paper_S01].\n\n{marker}\n"
    )
    draft_path, packet_path = _write_section(
        root,
        "S01",
        "First",
        draft,
        claims=[claim],
        evidence=evidence,
    )
    manifest = _write_manifest(
        root,
        [
            {
                "section_id": "S01",
                "english_draft_path": str(draft_path),
                "input_packet_path": str(packet_path),
            }
        ],
    )
    provider = Provider()
    summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=tmp_path / "out",
        role_provider=provider,
    )
    assert summary["status"] == "completed"
    assert len(provider.calls) == 5
    for call in provider.calls:
        section = call["payload"]["canonical_context"]["sections"][0]
        assert section["draft_text"].rstrip().endswith(marker)
        assert section["section_contract"]["section_purpose"]
        assert (
            section["section_governance"]["current_section_boundary_contract"][
                "section_id"
            ]
            == "S01"
        )
        assert section["paragraphs"][0]["hash"]
        for forbidden in (
            "evidence_packets",
            "claims",
            "claims_full",
            "manuscript_context",
            "open_questions",
        ):
            assert forbidden not in section
        assert "reviewer_comments_retained" not in section
        assert "open_questions" not in section["section_contract"]
        if call["payload"]["role"] == "commander_synthesis":
            assert "patch_blocks" in call["payload"]["canonical_context"]
            patch_blocks = call["payload"]["canonical_context"]["patch_blocks"]
            assert patch_blocks["S01-P01"]["text"]
            assert patch_blocks["S01-P01"]["hash"]
        else:
            assert "patch_blocks" not in call["payload"]["canonical_context"]
    canonical = json.loads(
        (tmp_path / "out" / "canonical_context.json").read_text(
            encoding="utf-8"
        )
    )
    canonical_section = canonical["sections"][0]
    assert canonical_section["evidence_packets"][0]["exact_spans"] == [quote]
    assert canonical_section["evidence_packets"][0]["limitations"] == (
        limitations
    )
    scope_contract = canonical_section["claims_full"][0][
        "claim_scope_contract"
    ]
    assert scope_contract["generality_ceiling"] == "bounded_benchmark"
    assert (
        "Do not extrapolate to all regimes."
        in scope_contract["prohibited_extrapolations"]
    )


def test_canonical_context_deduplicates_noise_without_losing_science(
    tmp_path: Path,
) -> None:
    root = tmp_path / "assets"
    duplicate_marker = "DUPLICATED_WORKPLAN_MARKER_" + ("w" * 1200)
    workplan = [
        {
            "section_id": f"S0{index}",
            "title": f"Workplan title {index}",
            "argument_role": f"Workplan role {index}",
            "must_cover": ["must-cover"] * 40,
        }
        for index in range(1, 4)
    ]
    workplan[0]["unique_workplan_id"] = duplicate_marker
    thesis = "GLOBAL_THESIS_MARKER_" + ("t" * 600)
    strategy = "GLOBAL_STRATEGY_MARKER_" + ("s" * 600)
    provenance = {
        f"chunk-{'a' * 40}-{index}": {
            "paper_id": f"paper_{index}",
            "provenance_type": "real_paper_id",
        }
        for index in range(8)
    }
    sections: list[dict[str, Any]] = []
    draft_markers: dict[str, str] = {}
    for section_id in ("S01", "S02", "S03"):
        marker = f"UNIQUE_DRAFT_MARKER_{section_id}_" + ("q" * 150)
        draft_markers[section_id] = marker
        draft = (
            f"# {section_id}\n\n"
            f"Paragraph [REF:paper_{section_id}].\n\n{marker}\n"
        )
        evidence = [
            {
                "claim_id": f"{section_id}-CL01",
                "chunk_id": f"chunk-{section_id}",
                "paper_id": f"paper_{section_id}",
                "source_title": f"Paper title {section_id}",
                "exact_spans": [
                    f"Exact quote for {section_id} with a [REF:paper_{section_id}]."
                ],
                "limitations": [f"Scope limitation for {section_id}."],
                "support_relation": "direct",
                "evidence_level": "fulltext",
                "source_kind": "s2_body",
                "scope_fit": "in_domain",
                "retrieval_role": "evidence_candidate",
                "provenance_type": "real_paper_id",
            }
        ]
        draft_path, packet_path = _write_section(
            root,
            section_id,
            f"Chapter {section_id}",
            draft,
            evidence=evidence,
            manuscript_context_overrides={
                "full_section_workplan": workplan,
                "global_review_thesis": thesis,
                "global_narrative_strategy": strategy,
                "evidence_provenance": provenance,
                "probe_report": {
                    "path": "outputs/probe.json",
                    "schema_version": "probe.v1",
                    "probe_timestamp": "2026-01-01T00:00:00Z",
                },
            },
        )
        sections.append(
            {
                "section_id": section_id,
                "english_draft_path": str(draft_path),
                "input_packet_path": str(packet_path),
            }
        )
    manifest = _write_manifest(root, sections)
    output = tmp_path / "out"
    summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=Provider(),
    )
    assert summary["status"] == "completed"
    canonical = json.loads(
        (output / "canonical_context.json").read_text(encoding="utf-8")
    )
    assert canonical["full_section_workplan"] == workplan
    assert canonical["global_review_thesis"] == thesis
    assert canonical["global_narrative_strategy"] == strategy
    for section in canonical["sections"]:
        manuscript_context = section["manuscript_context"]
        for noisy_key in (
            "full_section_workplan",
            "global_review_thesis",
            "global_narrative_strategy",
            "evidence_provenance",
            "probe_report",
        ):
            assert noisy_key not in manuscript_context
        section_id = section["section_id"]
        assert section["draft_text"].rstrip().endswith(
            draft_markers[section_id]
        )
        assert section["evidence_packets"][0]["exact_spans"][0] == (
            f"Exact quote for {section_id} with a "
            f"[REF:paper_{section_id}]."
        )
        assert section["evidence_packets"][0]["limitations"] == [
            f"Scope limitation for {section_id}."
        ]

    view = _role_context_view(canonical)
    view_json = json.dumps(view, ensure_ascii=False, separators=(",", ":"))
    assert duplicate_marker not in view_json
    assert view_json.count(thesis) == 1
    assert view_json.count(strategy) == 1
    assert "fingerprint" not in view
    assert "full_section_workplan" not in view
    for section in view["sections"]:
        assert all("text" not in paragraph for paragraph in section["paragraphs"])
        assert section["draft_text"].rstrip().endswith(
            draft_markers[section["section_id"]]
        )
        for forbidden in (
            "evidence_packets",
            "claims",
            "claims_full",
            "manuscript_context",
            "open_questions",
        ):
            assert forbidden not in section
        assert section["section_contract"]["section_purpose"]
        assert section["section_governance"][
            "current_section_boundary_contract"
        ]["section_id"] == section["section_id"]

    raw_total = 0
    for section in sections:
        packet = json.loads(
            Path(section["input_packet_path"]).read_text(encoding="utf-8")
        )
        raw_total += len(
            json.dumps(
                packet,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        raw_total += len(
            Path(section["english_draft_path"]).read_text(encoding="utf-8")
        )
    assert len(view_json) < raw_total


def test_qwen_provider_accounts_cost_from_tokens_via_cost_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_call_qwen_chat(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "content": "{}",
            "_llm_usage": {
                "model_name": "qwen3.7-flash",
                "input_tokens": 20000,
                "output_tokens": 5000,
                "token_usage_source": "provider_response",
                "mock_llm": False,
                "success": True,
                # Deliberately absent/zero: the provider must not trust it.
                "estimated_cost_cny": 0.0,
            },
        }

    monkeypatch.setattr(
        qwen_client_module, "call_qwen_chat", fake_call_qwen_chat
    )
    provider = QwenRoleProvider(prompts_dir=DEFAULT_PROMPTS_DIR)
    result = provider(
        "structure_strategist",
        {
            "role": "structure_strategist",
            "model_tier": "c2_model",
            "role_instructions": "instructions",
            "canonical_context": {},
        },
    )
    usage = result["usage"]
    assert usage["input_tokens"] == 20000
    assert usage["output_tokens"] == 5000
    expected_cost = 20000 / 1_000_000 * 0.2 + 5000 / 1_000_000 * 0.8
    assert usage["estimated_cost_cny"] == pytest.approx(expected_cost)
    assert usage["estimated_cost_cny"] > 0
    assert usage["cost_provenance"] == "configured_model_rate"


def test_runtime_normalizes_missing_cost_from_tokens(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)

    def structure_responder(
        role: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        content = DeterministicRoleProvider()(role, payload)
        return {
            "content": content,
            "usage": {
                "call_count": 1,
                "api_call_count": 1,
                "model_tier": "c2_model",
                "actual_model": "qwen3.7-flash",
                "input_tokens": 20000,
                "output_tokens": 5000,
                "token_usage_source": "provider_response",
            },
        }

    provider = Provider(
        responders={"structure_strategist": structure_responder}
    )
    output = tmp_path / "out"
    summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=provider,
    )
    assert summary["status"] == "completed"
    reviews = json.loads(
        (output / "role_reviews.json").read_text(encoding="utf-8")
    )
    usage = reviews["roles"]["structure_strategist"]["usage"]
    expected_cost = 20000 / 1_000_000 * 0.2 + 5000 / 1_000_000 * 0.8
    assert usage["estimated_cost_cny"] == pytest.approx(expected_cost)
    assert usage["estimated_cost_cny"] > 0
    assert usage["cost_provenance"] == "configured_model_rate"


def test_commander_incomplete_contract_fails_honestly(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)

    def commander_responder(
        role: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "manuscript_diagnosis": "",
            "proposed_section_order": [],
            "section_decisions": [],
            "cross_section_conflicts": [],
        }

    provider = Provider(
        responders={"commander_synthesis": commander_responder}
    )
    output = tmp_path / "out"
    summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=provider,
    )
    assert summary["status"] == "failed"
    assert "commander_synthesis" in summary["error"]
    work_order = json.loads(
        (output / "global_commander_work_order.json").read_text(
            encoding="utf-8"
        )
    )
    assert work_order["status"] == "failed"
    assert work_order["read_only_declaration"]["chapter_text_changed"] is False
    assert work_order["read_only_declaration"]["retrieval_launched"] is False
    reviews = json.loads(
        (output / "role_reviews.json").read_text(encoding="utf-8")
    )
    assert reviews["roles"]["commander_synthesis"]["status"] == "failed"


def test_resume_refuses_changed_input_packet(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)
    output = tmp_path / "out"
    first_summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=Provider(),
    )
    assert first_summary["status"] == "completed"
    packet_path = Path(sections[0]["input_packet_path"])
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    packet["claims"][0]["statement"] = (
        packet["claims"][0]["statement"] + " changed after run"
    )
    packet_path.write_text(
        json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def boom(role: str, payload: dict[str, Any]) -> Any:
        raise AssertionError("provider must not be called")

    with pytest.raises(ResumeFingerprintMismatch, match="fingerprint"):
        run_global_manuscript_commander(
            manifest_path=manifest,
            output_dir=output,
            resume=True,
            role_provider=boom,
        )


def test_resume_refuses_mode_and_tier_changes(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)
    output = tmp_path / "out"
    first_summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=Provider(),
        model_tier="c2_model",
    )
    assert first_summary["status"] == "completed"

    def boom(role: str, payload: dict[str, Any]) -> Any:
        raise AssertionError("provider must not be called")

    with pytest.raises(ResumeFingerprintMismatch, match="mode"):
        run_global_manuscript_commander(
            manifest_path=manifest,
            output_dir=output,
            resume=True,
            live=True,
            role_provider=boom,
        )
    with pytest.raises(ResumeFingerprintMismatch, match="tier"):
        run_global_manuscript_commander(
            manifest_path=manifest,
            output_dir=output,
            resume=True,
            model_tier="standard_model",
            role_provider=boom,
        )


def test_sanitize_drops_unknown_ids_without_contaminating_authoritative_context(
    tmp_path: Path,
) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)
    canonical = build_canonical_context(
        manifest, sections, fingerprint="test-fingerprint"
    )
    canonical_before = json.dumps(
        canonical, ensure_ascii=True, sort_keys=True
    )
    result = {
        "citation_audit": [
            {
                "section_id": "S99",
                "paragraph_id": "S99-P01",
                "ref_marker": "paper_S01",
                "paper_id": "paper_S01",
                "status": "verified",
                "note": "unknown section",
            },
            {
                "section_id": "S01",
                "paragraph_id": "S01-P99",
                "ref_marker": "paper_S01",
                "paper_id": "paper_S01",
                "status": "verified",
                "note": "unknown paragraph",
            },
            {
                "section_id": "S01",
                "paragraph_id": "S01-P01",
                "ref_marker": "paper_S01",
                "paper_id": "paper_S01",
                "status": "verified",
                "note": "valid",
            },
        ],
        "attribution_issues": [
            {
                "section_id": "S01",
                "claim_id": "S01-GHOST",
                "issue": "unknown claim id",
                "recommendation": "",
            }
        ],
        "retrieval_gap_proposals": [
            {
                "gap_type": "claim_evidence_gap",
                "section_id": "S01",
                "claim_id": "S01-GHOST",
                "reason": "unknown claim id",
                "proposed_retrieval_scope": "",
            }
        ],
    }
    cleaned, issues, usable = sanitize_role_result(
        "evidence_attribution_critic", result, canonical
    )
    assert usable is True
    assert [item["paragraph_id"] for item in cleaned["citation_audit"]] == [
        "S01-P01"
    ]
    assert cleaned["attribution_issues"] == []
    assert cleaned["retrieval_gap_proposals"] == []
    assert any("unknown section_id 'S99'" in issue for issue in issues)
    assert any(
        "unknown paragraph_id 'S01-P99'" in issue for issue in issues
    )
    assert any("unknown claim_id 'S01-GHOST'" in issue for issue in issues)
    canonical_after = json.dumps(
        canonical, ensure_ascii=True, sort_keys=True
    )
    assert canonical_after == canonical_before


def test_retrieval_proposals_never_enqueued(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)

    def commander_responder(
        role: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        base = DeterministicRoleProvider()(role, payload)
        base["retrieval_gap_proposals"] = [
            {
                "gap_type": "section_claim_gap",
                "section_id": "S01",
                "reason": "section axis lacks claims",
                "proposed_retrieval_scope": "future authorized search",
                "enqueue": True,
                "launch": "now",
            }
        ]
        return base

    provider = Provider(
        responders={"commander_synthesis": commander_responder}
    )
    output = tmp_path / "out"
    summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=provider,
    )
    assert summary["status"] == "completed"
    assert summary["retrieval_gap_proposal_count"] == 1
    work_order = json.loads(
        (output / "global_commander_work_order.json").read_text(
            encoding="utf-8"
        )
    )
    proposal = work_order["retrieval_gap_proposals"][0]
    assert proposal["gap_type"] == "section_claim_gap"
    assert "enqueue" not in proposal
    assert "launch" not in proposal
    proposal_text = json.dumps(work_order["retrieval_gap_proposals"])
    assert '"enqueue"' not in proposal_text
    assert "launch" not in proposal_text
    for call in provider.calls:
        payload_text = json.dumps(call["payload"])
        assert '"enqueue"' not in payload_text
        assert "launch_retrieval" not in payload_text
        assert "execute_retrieval" not in payload_text


def test_cited_vs_evidence_paper_ledgers_and_cited_audit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "assets"
    evidence_by_section = {
        "S01": ["paper_A", "paper_B", "paper_D"],
        "S02": ["paper_A", "paper_C", "paper_D"],
    }
    sections: list[dict[str, Any]] = []
    for section_id in ("S01", "S02"):
        draft = (
            f"# {section_id}\n\n"
            f"Paragraph citing [REF:paper_A].\n\n"
            f"UNIQUE_CITED_MARKER_{section_id}_\n"
        )
        evidence = [
            {
                "claim_id": f"{section_id}-CL01",
                "chunk_id": f"chunk-{section_id}-{paper_id}",
                "paper_id": paper_id,
                "source_title": f"Paper title {paper_id}",
                "exact_spans": [f"Exact span for {paper_id}."],
                "limitations": [f"Limitation for {paper_id}."],
                "support_relation": "direct",
                "evidence_level": "fulltext",
                "source_kind": "s2_body",
                "scope_fit": "in_domain",
                "retrieval_role": "evidence_candidate",
                "provenance_type": "real_paper_id",
            }
            for paper_id in evidence_by_section[section_id]
        ]
        draft_path, packet_path = _write_section(
            root,
            section_id,
            f"Chapter {section_id}",
            draft,
            evidence=evidence,
        )
        sections.append(
            {
                "section_id": section_id,
                "english_draft_path": str(draft_path),
                "input_packet_path": str(packet_path),
            }
        )
    manifest = _write_manifest(root, sections)
    provider = Provider()
    output = tmp_path / "out"
    summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=provider,
    )
    assert summary["status"] == "completed"
    canonical = json.loads(
        (output / "canonical_context.json").read_text(encoding="utf-8")
    )
    assert set(canonical["papers"]) == {
        "paper_A",
        "paper_B",
        "paper_C",
        "paper_D",
    }
    assert set(canonical["cited_papers"]) == {"paper_A"}
    assert canonical["cited_papers"]["paper_A"]["citation_count"] == 2
    assert canonical["cited_papers"]["paper_A"]["sections"] == ["S01", "S02"]
    assert set(canonical["cross_section_overlap"]) == {"paper_A"}
    assert canonical["cross_section_overlap"]["paper_A"]["section_pairs"] == [
        ["S01", "S02"]
    ]
    assert set(canonical["evidence_cross_section_overlap"]) == {
        "paper_A",
        "paper_D",
    }
    assert canonical["paper_count"] == 1
    assert canonical["evidence_paper_count"] == 4
    assert canonical["cross_section_overlap_count"] == 1
    assert canonical["evidence_cross_section_overlap_count"] == 2
    assert summary["paper_count"] == 1
    assert summary["evidence_paper_count"] == 4
    assert summary["cross_section_overlap_count"] == 1
    assert summary["evidence_cross_section_overlap_count"] == 2
    assert "cited_papers" in canonical["paper_ledger_semantics"]

    view = provider.calls[0]["payload"]["canonical_context"]
    assert set(view["papers"]) == {
        "paper_A",
        "paper_B",
        "paper_C",
        "paper_D",
    }
    assert set(view["cited_papers"]) == {"paper_A"}
    assert set(view["cross_section_overlap"]) == {"paper_A"}
    assert set(view["evidence_cross_section_overlap"]) == {
        "paper_A",
        "paper_D",
    }
    assert "cited_papers" in view["paper_ledger_semantics"]

    reviews = json.loads(
        (output / "role_reviews.json").read_text(encoding="utf-8")
    )
    structure = reviews["roles"]["structure_strategist"]["result"]
    editor = reviews["roles"]["scientific_synthesis_editor"]["result"]
    critic = reviews["roles"]["evidence_attribution_critic"]["result"]
    assert [item["paper_id"] for item in structure["repeated_paper_roles"]] == [
        "paper_A"
    ]
    assert [item["paper_id"] for item in editor["repeated_paper_roles"]] == [
        "paper_A"
    ]
    assert [item["paper_id"] for item in critic["source_concentration"]] == [
        "paper_A"
    ]
    assert {
        item["paper_id"] for item in critic["citation_audit"]
    } == {"paper_A"}

    # Sanitization must still accept uncited evidence candidates so they can
    # be discussed in gap/revision proposals.
    cleaned, issues, usable = sanitize_role_result(
        "evidence_attribution_critic",
        {
            "source_concentration": [
                {
                    "paper_id": "paper_D",
                    "title": "Paper title paper_D",
                    "sections": ["S01", "S02"],
                    "concentration": "high",
                    "recommendation": "candidate-only overlap",
                }
            ]
        },
        canonical,
    )
    assert usable is True
    assert [item["paper_id"] for item in cleaned["source_concentration"]] == [
        "paper_D"
    ]
    assert not any("paper_D" in issue for issue in issues)


def test_word_count_metric_provenance(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)
    provider = Provider()
    output = tmp_path / "out"
    summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=provider,
    )
    assert summary["status"] == "completed"
    canonical = json.loads(
        (output / "canonical_context.json").read_text(encoding="utf-8")
    )
    assert canonical["word_count_metric"] == "whitespace_split_units"
    assert "str.split" in canonical["word_count_definition"]
    assert canonical["total_word_count"] == sum(
        section["word_count"] for section in canonical["sections"]
    )
    for section in canonical["sections"]:
        assert section["word_count"] == sum(
            paragraph["word_count"] for paragraph in section["paragraphs"]
        )
        draft = Path(section["english_draft_path"]).read_text(
            encoding="utf-8"
        )
        expected = sum(
            _word_count(block) for block in _split_paragraphs(draft)
        )
        assert section["word_count"] == expected
    assert summary["word_count_metric"] == "whitespace_split_units"
    assert "str.split" in summary["word_count_definition"]
    view = provider.calls[0]["payload"]["canonical_context"]
    assert view["word_count_metric"] == "whitespace_split_units"
    assert "str.split" in view["word_count_definition"]
    assert view["total_word_count"] == canonical["total_word_count"]


# ---------------------------------------------------------------------------
# M4 integration contract: patch safety gate
# ---------------------------------------------------------------------------


def _m4_sections_from_manifest(
    sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for section in sections:
        draft = Path(section["english_draft_path"]).read_text(
            encoding="utf-8"
        )
        packet = json.loads(
            Path(section["input_packet_path"]).read_text(encoding="utf-8")
        )
        result.append(
            {
                "section_id": section["section_id"],
                "draft_text": draft,
                "input_packet": packet,
            }
        )
    return result


def _m4_move_patch(
    snapshot: dict[str, Any],
    *,
    patch_id: str = "M4-P01",
    block_index: int = 0,
) -> dict[str, Any]:
    first_section = snapshot["section_ids"][0]
    destination = snapshot["section_ids"][1]
    target = snapshot["block_order"][first_section][
        min(block_index, len(snapshot["block_order"][first_section]) - 1)
    ]
    block = snapshot["blocks"][target]
    return {
        "patch_id": patch_id,
        "operation_type": "move_block",
        "target_section_id": first_section,
        "target_block_id": target,
        "destination_section_id": destination,
        "base_hash": block["hash"],
        "source_hash": block["hash"],
        "claims_before": list(block.get("contract_claim_ids") or []),
        "claims_after": list(block.get("contract_claim_ids") or []),
        "evidence_before": [],
        "evidence_after": [],
        "ownership_before": [first_section],
        "ownership_after": [destination],
        "claim_strength_change": "none",
        "citation_change": "none",
        "collateral_block_ids": [],
        "invariants": [
            "no_scientific_meaning_change",
            "preserve_sibling_boundaries",
        ],
        "risk": "none",
        "approval_required": False,
    }


def _m4_sections_with_packet_override(
    sections: list[dict[str, Any]],
    section_id: str,
    mutator: Callable[[dict[str, Any]], None],
) -> list[dict[str, Any]]:
    result = _m4_sections_from_manifest(sections)
    for section in result:
        if section["section_id"] == section_id:
            packet = section["input_packet"]
            mutator(packet)
    return result


def _m4_sections_with_draft_override(
    sections: list[dict[str, Any]],
    section_id: str,
    text: str,
) -> list[dict[str, Any]]:
    result = _m4_sections_from_manifest(sections)
    for section in result:
        if section["section_id"] == section_id:
            section["draft_text"] = text
    return result


def _m4_rewrite_patch(
    snapshot: dict[str, Any],
    *,
    patch_id: str = "M4-P02",
    after_text: str = "Approved transition rewrite text.",
) -> dict[str, Any]:
    first_section = snapshot["section_ids"][0]
    target = snapshot["block_order"][first_section][0]
    block = snapshot["blocks"][target]
    return {
        "patch_id": patch_id,
        "operation_type": "rewrite_transition",
        "target_section_id": first_section,
        "target_block_id": target,
        "base_hash": block["hash"],
        "block_text_after": after_text,
        "reason": "Rewrite weak transition.",
        "finding_ids": ["GMC-ED-001"],
        "claims_before": list(block.get("contract_claim_ids") or []),
        "claims_after": list(block.get("contract_claim_ids") or []),
        "evidence_before": [],
        "evidence_after": [],
        "ownership_before": [first_section],
        "ownership_after": [first_section],
        "claim_strength_change": "none",
        "citation_change": "none",
        "collateral_block_ids": [],
        "invariants": ["no_scientific_meaning_change"],
        "risk": "medium",
        "approval_required": True,
    }


def test_m4_patch_set_rejects_stale_hash(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)
    snapshot = build_m4_snapshot(
        _m4_sections_from_manifest(sections), fingerprint="fp-1"
    )
    patch = _m4_move_patch(snapshot)
    patch["base_hash"] = "0" * 64
    validation = validate_m4_patch_set(snapshot, [patch])
    assert validation["status"] == "rejected"
    assert any("stale base_hash" in error for error in validation["errors"])
    assert validation["rejected_patches"][0]["patch_id"] == "M4-P01"


def test_m4_unauthorized_semantic_patch_behavior(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)
    snapshot = build_m4_snapshot(
        _m4_sections_from_manifest(sections), fingerprint="fp-2"
    )
    patch = _m4_rewrite_patch(snapshot)
    first_round = validate_m4_patch_set(snapshot, [patch], approvals=None)
    assert first_round["status"] == "awaiting_approval"
    assert first_round["awaiting_patches"][0]["patch_id"] == "M4-P02"
    # A decision round that omits the patch is a missing-authorization reject.
    missing = validate_m4_patch_set(snapshot, [patch], approvals={})
    assert missing["status"] == "rejected"
    assert any(
        "missing authorization" in error for error in missing["errors"]
    )
    # Declining everything must leave the snapshot byte-identical.
    declined = apply_m4_patch_set(
        snapshot, [patch], approvals={"M4-P02": "declined"}
    )
    assert declined["status"] == "noop"
    assert declined["byte_identical"] is True
    assert declined["post_snapshot_hash"] == declined["base_snapshot_hash"]
    # Explicit approval of the semantic patch applies it to a new version.
    approved = apply_m4_patch_set(
        snapshot, [patch], approvals={"M4-P02": "approved"}
    )
    assert approved["status"] == "applied"
    assert approved["post_snapshot_hash"] != approved["base_snapshot_hash"]
    assert "Approved transition rewrite text." in approved[
        "new_text_by_section"
    ][snapshot["section_ids"][0]]


def test_m4_safe_deterministic_move_applies_and_ledger_passes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "assets"
    manifest, sections, markers = _three_sections(root)
    # The moved block contains the destination chapter title: deterministic
    # positive destination-fit evidence.
    snapshot = build_m4_snapshot(
        _m4_sections_with_draft_override(
            sections,
            "S01",
            "# First Chapter\n\n"
            "Opening paragraph of S01 with [REF:paper_S01].\n\n"
            "Second Chapter synthesis paragraph.\n",
        ),
        fingerprint="fp-3",
    )
    patch = _m4_move_patch(snapshot, block_index=2)
    validation = validate_m4_patch_set(snapshot, [patch])
    assert validation["status"] == "valid"
    report = validation["patch_reports"][0]
    assert report["ownership_compliance"] == "proven"
    assert report["boundary_compliance"] == "proven"
    result = apply_m4_patch_set(snapshot, [patch])
    assert result["status"] == "applied"
    assert result["byte_identical"] is False
    assert result["changed_sections"] == [
        snapshot["section_ids"][0],
        snapshot["section_ids"][1],
    ]
    assert result["post_snapshot_hash"] != result["base_snapshot_hash"]
    assert result["applied_patches"][0]["operation_type"] == "move_block"
    assert result["applied_patches"][0]["before_block_hash"] == (
        result["applied_patches"][0]["after_block_hash"]
    )
    moved_text = snapshot["blocks"][patch["target_block_id"]]["text"]
    assert moved_text in result["new_text_by_section"][
        snapshot["section_ids"][1]
    ]
    assert moved_text not in result["new_text_by_section"][
        snapshot["section_ids"][0]
    ]
    ledger = audit_m4_claim_evidence_ledger(
        snapshot, result["new_text_by_section"]
    )
    assert ledger["status"] == "passed"


def test_m4_move_unique_contribution_match_is_positive_fit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)
    # Matching the destination unique_contribution is positive ownership
    # evidence, never a boundary violation.
    snapshot = build_m4_snapshot(
        _m4_sections_with_draft_override(
            sections,
            "S01",
            "# First Chapter\n\n"
            "Unique contribution for S02 belongs in this paragraph.\n",
        ),
        fingerprint="fp-positive",
    )
    patch = _m4_move_patch(snapshot, block_index=1)
    validation = validate_m4_patch_set(snapshot, [patch], approvals=None)
    assert validation["status"] == "valid"
    assert validation["patch_reports"][0]["boundary_compliance"] == "proven"
    result = apply_m4_patch_set(snapshot, [patch])
    assert result["status"] == "applied"


def test_m4_move_unproven_fit_requires_approval_then_applies(
    tmp_path: Path,
) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)
    # must_not_cover data exists and is not violated, but the moved block
    # matches no positive destination-fit evidence: unproven, never auto-apply.
    snapshot = build_m4_snapshot(
        _m4_sections_from_manifest(sections), fingerprint="fp-unproven"
    )
    patch = _m4_move_patch(snapshot)
    auto = validate_m4_patch_set(snapshot, [patch], approvals=None)
    assert auto["status"] == "awaiting_approval"
    assert auto["patch_reports"][0]["boundary_compliance"] == "unproven"
    approved = validate_m4_patch_set(
        snapshot, [patch], approvals={"M4-P01": "approved"}
    )
    assert approved["status"] == "valid"
    result = apply_m4_patch_set(
        snapshot, [patch], approvals={"M4-P01": "approved"}
    )
    assert result["status"] == "applied"


def test_m4_move_requires_exact_ownership(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)
    snapshot = build_m4_snapshot(
        _m4_sections_from_manifest(sections), fingerprint="fp-own"
    )
    missing = _m4_move_patch(snapshot)
    missing.pop("ownership_before")
    missing.pop("ownership_after")
    validation = validate_m4_patch_set(snapshot, [missing])
    assert validation["status"] == "rejected"
    assert any(
        "ownership_before must be exactly" in error
        for error in validation["errors"]
    )
    assert any(
        "ownership_after must be exactly" in error
        for error in validation["errors"]
    )
    # A mismatched ownership assertion cannot be rescued by approval.
    mismatched = _m4_move_patch(snapshot)
    mismatched["ownership_before"] = ["S02"]
    approved = validate_m4_patch_set(
        snapshot, [mismatched], approvals={"M4-P01": "approved"}
    )
    assert approved["status"] == "rejected"
    assert any(
        "ownership_before must be exactly" in error
        for error in approved["errors"]
    )


def test_m4_move_boundary_data_absent_requires_approval(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)

    def strip_boundary(packet: dict[str, Any]) -> None:
        contract = packet["section_contract"]
        contract.pop("must_not_cover", None)
        contract.pop("unique_contribution", None)

    snapshot = build_m4_snapshot(
        _m4_sections_with_packet_override(sections, "S02", strip_boundary),
        fingerprint="fp-boundary",
    )
    patch = _m4_move_patch(snapshot)
    auto = validate_m4_patch_set(snapshot, [patch], approvals=None)
    assert auto["status"] == "awaiting_approval"
    assert auto["patch_reports"][0]["boundary_compliance"] == "unproven"
    approved = validate_m4_patch_set(
        snapshot, [patch], approvals={"M4-P01": "approved"}
    )
    assert approved["status"] == "valid"
    result = apply_m4_patch_set(
        snapshot, [patch], approvals={"M4-P01": "approved"}
    )
    assert result["status"] == "applied"


def test_m4_move_destination_must_not_cover_rejects_even_with_approval(
    tmp_path: Path,
) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)

    def prohibit_heading(packet: dict[str, Any]) -> None:
        packet["section_contract"]["must_not_cover"] = ["First Chapter"]

    snapshot = build_m4_snapshot(
        _m4_sections_with_packet_override(
            sections, "S02", prohibit_heading
        ),
        fingerprint="fp-contradiction",
    )
    patch = _m4_move_patch(snapshot)
    validation = validate_m4_patch_set(
        snapshot, [patch], approvals={"M4-P01": "approved"}
    )
    assert validation["status"] == "rejected"
    assert any(
        "destination boundary contradiction" in error
        for error in validation["errors"]
    )
    assert validation["patch_reports"][0]["boundary_compliance"] == "failed"


def test_deterministic_dry_commander_proposes_empty_patch_set(
    tmp_path: Path,
) -> None:
    root = tmp_path / "assets"
    manifest, sections, _ = _three_sections(root)
    summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=tmp_path / "out",
        role_provider=Provider(),
    )
    assert summary["status"] == "completed"
    assert summary["m4_patch_count"] == 0
    work_order = json.loads(
        (tmp_path / "out" / "global_commander_work_order.json").read_text(
            encoding="utf-8"
        )
    )
    assert work_order["proposed_patch_set"] == []
    assert work_order["read_only_declaration"]["chapter_text_changed"] is False


def test_manifest_optional_ledger_validated_only_when_supplied(
    tmp_path,
) -> None:
    draft_path, packet_path = _write_section(
        tmp_path, "S01", "One", "# S01\n\n[REF:paper_S01]\n"
    )
    base = {
        "section_id": "S01",
        "english_draft_path": str(draft_path),
        "input_packet_path": str(packet_path),
    }
    loaded = load_manifest(_write_manifest(tmp_path, [base]))
    assert "explanatory_citation_ledger_path" not in loaded[0]

    ledger = _write_ledger(tmp_path, "S01", [])
    with_ledger = dict(base)
    with_ledger["explanatory_citation_ledger_path"] = str(ledger)
    loaded2 = load_manifest(_write_manifest(tmp_path, [with_ledger]))
    assert loaded2[0]["explanatory_citation_ledger_path"] == str(ledger)

    bad = dict(base)
    bad["explanatory_citation_ledger_path"] = str(
        tmp_path / "missing_ledger.json"
    )
    with pytest.raises(ManifestError, match="explanatory citation ledger missing"):
        load_manifest(_write_manifest(tmp_path, [bad]))


def test_fingerprint_includes_explanatory_ledger(tmp_path) -> None:
    draft_path, packet_path = _write_section(
        tmp_path, "S01", "One", "# S01\n\n[REF:bg]\n"
    )
    base = {
        "section_id": "S01",
        "english_draft_path": str(draft_path),
        "input_packet_path": str(packet_path),
    }
    without = compute_fingerprint(
        _write_manifest(tmp_path, [base]), [base]
    )

    ledger = _write_ledger(
        tmp_path,
        "S01",
        [{"marker_id": "bg", "metadata": {"title": "B", "paper_id": "s2:bg"}}],
    )
    with_ledger = dict(base)
    with_ledger["explanatory_citation_ledger_path"] = str(ledger)
    with_ledger_fp = compute_fingerprint(
        _write_manifest(tmp_path, [with_ledger]), [with_ledger]
    )
    assert without != with_ledger_fp

    ledger2 = _write_ledger(
        tmp_path,
        "S01",
        [{"marker_id": "bg2", "metadata": {"title": "C"}}],
    )
    with_ledger2 = dict(base)
    with_ledger2["explanatory_citation_ledger_path"] = str(ledger2)
    changed = compute_fingerprint(
        _write_manifest(tmp_path, [with_ledger2]), [with_ledger2]
    )
    assert changed != with_ledger_fp


def test_explanatory_ledger_resolution_and_no_promotion(tmp_path) -> None:
    draft_s01 = "# S01\n\n[REF:background_ref_1] [REF:paper_S01]\n"
    draft_s02 = "# S02\n\n[REF:background_ref_1] [REF:paper_S02]\n"
    dp1, pp1 = _write_section(tmp_path, "S01", "One", draft_s01)
    dp2, pp2 = _write_section(tmp_path, "S02", "Two", draft_s02)
    ledger = _write_ledger(
        tmp_path,
        "S01",
        [
            {
                "marker_id": "background_ref_1",
                "handle": "H1",
                "permission": "background_explanation_only",
                "metadata": {
                    "title": "Background Paper",
                    "paper_id": "s2:bg1",
                },
            }
        ],
    )
    sections = [
        {
            "section_id": "S01",
            "english_draft_path": str(dp1),
            "input_packet_path": str(pp1),
            "explanatory_citation_ledger_path": str(ledger),
        },
        {
            "section_id": "S02",
            "english_draft_path": str(dp2),
            "input_packet_path": str(pp2),
        },
    ]
    canonical = build_canonical_context(
        _write_manifest(tmp_path, sections), sections
    )
    rim = canonical["ref_identity_map"]
    assert rim["background_ref_1"]["known"] is True
    assert rim["background_ref_1"]["trust_type"] == (
        "background_explanation_only"
    )
    assert rim["background_ref_1"]["title"] == "Background Paper"
    assert rim["paper_S01"]["trust_type"] == "core_evidence"
    assert "s2:bg1" not in canonical["papers"]
    assert canonical["evidence_paper_count"] == 2
    assert canonical["sections"][0]["evidence_packet_count"] == 1
    assert len(canonical["sections"][0]["evidence_packets"]) == 1
    explanatory = canonical["explanatory_papers"]["s2:bg1"]
    assert explanatory["trust_type"] == "background_explanation_only"
    assert explanatory["evidence_chunk_count"] == 0
    cited = canonical["cited_papers"]["s2:bg1"]
    assert cited["trust_type"] == "background_explanation_only"
    assert cited["citation_count"] == 2
    assert cited["section_count"] == 2
    assert "s2:bg1" in canonical["cross_section_overlap"]
    assert canonical["sections"][0][
        "explanatory_citation_ledger_records"
    ][0]["trust_type"] == "background_explanation_only"
    assert canonical["sections"][1][
        "explanatory_citation_ledger_records"
    ] == []


def test_core_explanatory_overlap_trust_authoritative(tmp_path) -> None:
    draft = "# S01\n\n[REF:background_ref_overlap] [REF:paper_S01]\n"
    dp, pp = _write_section(tmp_path, "S01", "One", draft)
    ledger = _write_ledger(
        tmp_path,
        "S01",
        [
            {
                "marker_id": "background_ref_overlap",
                "handle": "H",
                "metadata": {
                    "title": "Core Paper",
                    "paper_id": "paper_S01",
                },
            }
        ],
    )
    sections = [
        {
            "section_id": "S01",
            "english_draft_path": str(dp),
            "input_packet_path": str(pp),
            "explanatory_citation_ledger_path": str(ledger),
        }
    ]
    canonical = build_canonical_context(
        _write_manifest(tmp_path, sections), sections
    )
    assert canonical["papers"]["paper_S01"]["primary_title"] == (
        "Paper title S01"
    )
    assert len(canonical["papers"]) == 1
    assert canonical["explanatory_papers"]["paper_S01"][
        "overlaps_core_reference"
    ] is True
    assert canonical["ref_identity_map"]["background_ref_overlap"][
        "trust_type"
    ] == "background_explanation_only"
    assert canonical["ref_identity_map"]["paper_S01"]["trust_type"] == (
        "core_evidence"
    )
    assert canonical["cited_papers"]["paper_S01"]["trust_type"] == (
        "core_evidence"
    )
    assert canonical["cited_papers"]["paper_S01"]["evidence_chunk_count"] == 1


def test_old_manifest_without_ledgers_unchanged(tmp_path) -> None:
    draft_path, packet_path = _write_section(
        tmp_path, "S01", "One", "# S01\n\n[REF:paper_S01]\n"
    )
    sections = [
        {
            "section_id": "S01",
            "english_draft_path": str(draft_path),
            "input_packet_path": str(packet_path),
        }
    ]
    canonical = build_canonical_context(
        _write_manifest(tmp_path, sections), sections
    )
    assert "explanatory_papers" not in canonical
    assert "explanation_ledgers" not in canonical
    assert "trust_type" not in canonical["ref_identity_map"]["paper_S01"]
    assert "explanatory_citation_ledger_records" not in canonical["sections"][0]


def test_m4_snapshot_ignores_ledger_paths(tmp_path) -> None:
    draft_text = "# S01\n\n[REF:bg] [REF:paper_S01]\n"
    draft_path, packet_path = _write_section(
        tmp_path, "S01", "One", draft_text
    )
    ledger = _write_ledger(
        tmp_path,
        "S01",
        [{"marker_id": "bg", "metadata": {"title": "B", "paper_id": "s2:bg"}}],
    )
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    base_sections = [
        {
            "section_id": "S01",
            "draft_text": draft_text,
            "input_packet": packet,
        }
    ]
    snap_no = build_m4_snapshot(base_sections)
    snap_with = build_m4_snapshot(
        [{**base_sections[0], "explanatory_citation_ledger_path": str(ledger)}]
    )
    assert snap_no["base_snapshot_hash"] == snap_with["base_snapshot_hash"]
    assert "explanatory_papers" not in snap_with["canonical"]
    assert "explanation_ledgers" not in snap_with["canonical"]


def test_role_context_view_includes_explanatory_trust_metadata(
    tmp_path,
) -> None:
    draft = "# S01\n\n[REF:background_ref_1] [REF:paper_S01]\n"
    dp, pp = _write_section(tmp_path, "S01", "One", draft)
    ledger = _write_ledger(
        tmp_path,
        "S01",
        [
            {
                "marker_id": "background_ref_1",
                "metadata": {
                    "title": "Background Paper",
                    "paper_id": "s2:bg1",
                },
            }
        ],
    )
    sections = [
        {
            "section_id": "S01",
            "english_draft_path": str(dp),
            "input_packet_path": str(pp),
            "explanatory_citation_ledger_path": str(ledger),
        }
    ]
    canonical = build_canonical_context(
        _write_manifest(tmp_path, sections), sections
    )
    view = _role_context_view(canonical)
    assert view["explanatory_paper_count"] == 1
    assert view["explanatory_marker_count"] >= 1
    entry = view["explanatory_papers"]["s2:bg1"]
    assert entry["trust_type"] == "background_explanation_only"
    assert entry["role"] == "background"
    assert entry["primary_title"] == "Background Paper"
    assert entry["sections"] == ["S01"]
    assert "background_ref_1" in entry["marker_ids"]
    assert "abstract" not in entry
    ledger_view = view["sections"][0][
        "explanatory_citation_ledger_records"
    ]
    assert ledger_view[0]["trust_type"] == "background_explanation_only"
    assert ledger_view[0]["role"] == "background"
    assert view["sections"][0]["evidence_packet_count"] == 1


def test_sanitize_preserves_explanatory_papers_without_promotion(
    tmp_path,
) -> None:
    draft_s01 = "# S01\n\n[REF:background_ref_1] [REF:paper_S01]\n"
    draft_s02 = "# S02\n\n[REF:background_ref_1] [REF:paper_S02]\n"
    dp1, pp1 = _write_section(tmp_path, "S01", "One", draft_s01)
    dp2, pp2 = _write_section(tmp_path, "S02", "Two", draft_s02)
    ledger = _write_ledger(
        tmp_path,
        "S01",
        [
            {
                "marker_id": "background_ref_1",
                "metadata": {
                    "title": "Background Paper",
                    "paper_id": "s2:bg1",
                },
            }
        ],
    )
    sections = [
        {
            "section_id": "S01",
            "english_draft_path": str(dp1),
            "input_packet_path": str(pp1),
            "explanatory_citation_ledger_path": str(ledger),
        },
        {
            "section_id": "S02",
            "english_draft_path": str(dp2),
            "input_packet_path": str(pp2),
        },
    ]
    canonical = build_canonical_context(
        _write_manifest(tmp_path, sections), sections
    )
    content = {
        "citation_audit": [
            {
                "section_id": "S01",
                "ref_marker": "background_ref_1",
                "paper_id": "s2:bg1",
                "status": "present",
            }
        ],
        "repeated_paper_roles": [
            {
                "paper_id": "s2:bg1",
                "title": "Background Paper",
                "sections": ["S01", "S02"],
                "roles": ["background"],
                "recommendation": "dedupe",
            }
        ],
        "overlap_recommendations": [
            {
                "sections": ["S01", "S02"],
                "paper_id": "s2:bg1",
                "recommendation": "dedupe",
            }
        ],
        "source_concentration": [
            {
                "paper_id": "s2:bg1",
                "title": "Background Paper",
                "sections": ["S01"],
                "concentration": "high",
            }
        ],
        "synthesis_findings": [
            {
                "section_id": "S01",
                "finding": "overlap note",
                "source_paper_ids": ["s2:bg1"],
            }
        ],
    }
    cleaned, issues, usable = sanitize_role_result(
        "scientific_synthesis_editor", content, canonical
    )
    assert usable is True
    assert cleaned["citation_audit"][0]["paper_id"] == "s2:bg1"
    assert cleaned["citation_audit"][0]["trust_type"] == (
        "background_explanation_only"
    )
    assert cleaned["repeated_paper_roles"][0]["paper_id"] == "s2:bg1"
    assert cleaned["repeated_paper_roles"][0]["trust_type"] == (
        "background_explanation_only"
    )
    assert cleaned["overlap_recommendations"][0]["paper_id"] == "s2:bg1"
    assert cleaned["overlap_recommendations"][0]["trust_type"] == (
        "background_explanation_only"
    )
    assert cleaned["source_concentration"][0]["paper_id"] == "s2:bg1"
    assert cleaned["source_concentration"][0]["trust_type"] == (
        "background_explanation_only"
    )
    assert cleaned["synthesis_findings"][0]["source_paper_ids"] == ["s2:bg1"]
    assert cleaned["synthesis_findings"][0]["source_paper_trust_types"] == {
        "s2:bg1": "background_explanation_only"
    }
    # Background papers never enter evidence packets or core counts.
    assert "s2:bg1" not in canonical["papers"]
    assert canonical["evidence_paper_count"] == 2
    assert canonical["sections"][0]["evidence_packet_count"] == 1
    assert all("s2:bg1" not in issue for issue in issues)


def test_ledger_present_order_independent_and_malformed_records(
    tmp_path,
) -> None:
    draft_s01 = "# S01\n\n[REF:background_ref_1] [REF:paper_S01]\n"
    draft_s02 = "# S02\n\n[REF:background_ref_1] [REF:paper_S02]\n"
    dp1, pp1 = _write_section(tmp_path, "S01", "One", draft_s01)
    dp2, pp2 = _write_section(tmp_path, "S02", "Two", draft_s02)
    ledger = _write_ledger(
        tmp_path,
        "S01",
        [
            {
                "marker_id": "background_ref_1",
                "metadata": {"title": "B", "paper_id": "s2:bg1"},
            }
        ],
    )
    ledger2 = _write_ledger(
        tmp_path,
        "S02",
        [
            {
                "marker_id": "background_ref_1",
                "metadata": {"title": "B", "paper_id": "s2:bg1"},
            }
        ],
    )
    sections_a = [
        {
            "section_id": "S01",
            "english_draft_path": str(dp1),
            "input_packet_path": str(pp1),
            "explanatory_citation_ledger_path": str(ledger),
        },
        {
            "section_id": "S02",
            "english_draft_path": str(dp2),
            "input_packet_path": str(pp2),
        },
    ]
    sections_b = [
        {
            "section_id": "S01",
            "english_draft_path": str(dp1),
            "input_packet_path": str(pp1),
        },
        {
            "section_id": "S02",
            "english_draft_path": str(dp2),
            "input_packet_path": str(pp2),
            "explanatory_citation_ledger_path": str(ledger2),
        },
    ]
    canonical_a = build_canonical_context(
        _write_manifest(tmp_path, sections_a), sections_a
    )
    canonical_b = build_canonical_context(
        _write_manifest(tmp_path, sections_b), sections_b
    )
    assert "explanatory_papers" in canonical_a
    assert "explanatory_papers" in canonical_b
    assert "trust_type" in canonical_a["ref_identity_map"]["paper_S01"]
    assert "trust_type" in canonical_b["ref_identity_map"]["paper_S01"]
    assert "explanatory_citation_ledger_records" in canonical_a["sections"][0]
    assert "explanatory_citation_ledger_records" in canonical_b["sections"][0]
    assert canonical_a["ref_identity_map"]["background_ref_1"][
        "trust_type"
    ] == "background_explanation_only"
    assert canonical_b["ref_identity_map"]["background_ref_1"][
        "trust_type"
    ] == "background_explanation_only"

    malformed = _write_ledger(tmp_path, "S01", [])
    malformed.write_text(
        json.dumps({"records": "not-a-list"}), encoding="utf-8"
    )
    sections_bad = [
        {
            "section_id": "S01",
            "english_draft_path": str(dp1),
            "input_packet_path": str(pp1),
            "explanatory_citation_ledger_path": str(malformed),
        }
    ]
    with pytest.raises(ManifestError, match="'records' list"):
        build_canonical_context(
            _write_manifest(tmp_path, sections_bad), sections_bad
        )

    with pytest.raises(ManifestError, match="explanatory_ledger must be a list"):
        build_canonical_context(
            "in-memory",
            [
                {
                    "section_id": "S01",
                    "english_draft_path": str(dp1),
                    "input_packet_path": str(pp1),
                }
            ],
            in_memory_sections={
                "S01": {
                    "draft_text": draft_s01,
                    "input_packet": {},
                    "explanatory_ledger": "bad",
                }
            },
        )


def test_marker_alias_conflict_and_handle_not_registered(tmp_path) -> None:
    draft = "# S01\n\n[REF:dup-marker] [REF:chapter-local-h] [REF:paper_S01]\n"
    dp, pp = _write_section(tmp_path, "S01", "One", draft)
    ledger_conflict = _write_ledger(
        tmp_path,
        "S01",
        [
            {
                "marker_id": "dup-marker",
                "metadata": {"paper_id": "s2:a"},
            },
            {
                "marker_id": "dup-marker",
                "metadata": {"paper_id": "s2:b"},
            },
        ],
    )
    sections_conflict = [
        {
            "section_id": "S01",
            "english_draft_path": str(dp),
            "input_packet_path": str(pp),
            "explanatory_citation_ledger_path": str(ledger_conflict),
        }
    ]
    with pytest.raises(ManifestError, match="explanatory marker alias conflict"):
        build_canonical_context(
            _write_manifest(tmp_path, sections_conflict), sections_conflict
        )

    handle_only = _write_ledger(
        tmp_path,
        "S01",
        [{"handle": "chapter-local-h", "metadata": {"title": "T"}}],
    )
    sections_handle = [
        {
            "section_id": "S01",
            "english_draft_path": str(dp),
            "input_packet_path": str(pp),
            "explanatory_citation_ledger_path": str(handle_only),
        }
    ]
    canonical = build_canonical_context(
        _write_manifest(tmp_path, sections_handle), sections_handle
    )
    assert canonical["ref_identity_map"]["chapter-local-h"]["known"] is False


def test_doi_and_s2_aliases_for_same_explanatory_paper_are_merged(tmp_path) -> None:
    draft = "# S01\n\n[REF:doi:10.1515/nanoph-2020-0291] [REF:s2:ef72b2bba48c4dea14b4416cd23b09d9145dc963]\n"
    dp, pp = _write_section(tmp_path, "S01", "One", draft)
    ledger = _write_ledger(
        tmp_path,
        "S01",
        [
            {
                "marker_id": "doi:10.1515/nanoph-2020-0291",
                "metadata": {
                    "paper_id": "doi:10.1515/nanoph-2020-0291",
                    "doi": "10.1515/NANOPH-2020-0291",
                    "title": "Misalignment resilient diffractive optical networks",
                },
            },
            {
                "marker_id": "s2:ef72b2bba48c4dea14b4416cd23b09d9145dc963",
                "metadata": {
                    "paper_id": "s2:ef72b2bba48c4dea14b4416cd23b09d9145dc963",
                    "doi": "10.1515/nanoph-2020-0291",
                    "title": "Misalignment resilient diffractive optical networks",
                },
            },
        ],
    )
    sections = [
        {
            "section_id": "S01",
            "english_draft_path": str(dp),
            "input_packet_path": str(pp),
            "explanatory_citation_ledger_path": str(ledger),
        }
    ]

    canonical = build_canonical_context(
        _write_manifest(tmp_path, sections), sections
    )

    assert len(canonical["explanatory_papers"]) == 1
    assert "doi:10.1515/nanoph-2020-0291" in canonical["explanatory_papers"]
    assert canonical["ref_identity_map"][
        "s2:ef72b2bba48c4dea14b4416cd23b09d9145dc963"
    ]["paper_id"] == "doi:10.1515/nanoph-2020-0291"
    assert canonical["ref_identity_map"]["doi:10.1515/nanoph-2020-0291"][
        "paper_id"
    ] == "doi:10.1515/nanoph-2020-0291"


def test_doi_and_s2_aliases_merge_when_s2_record_lacks_doi(tmp_path) -> None:
    """A bare S2 id without a DOI adopts the DOI identity of the same paper.

    This mirrors the real ledger split where one section records the paper
    only by its Semantic Scholar id while another section records the DOI
    next to the ``s2:``-prefixed id of the same paper.
    """
    draft = (
        "# S01\n\n[REF:e7cda7beaa70f65e43b46d813b939ffd92b9b0eb] "
        "[REF:s2:e7cda7beaa70f65e43b46d813b939ffd92b9b0eb]\n"
    )
    dp, pp = _write_section(tmp_path, "S01", "One", draft)
    ledger = _write_ledger(
        tmp_path,
        "S01",
        [
            {
                "marker_id": "e7cda7beaa70f65e43b46d813b939ffd92b9b0eb",
                "metadata": {
                    "paper_id": "e7cda7beaa70f65e43b46d813b939ffd92b9b0eb",
                    "title": "Aerogel-Functionalized Thermoplastic Polyurethane",
                },
            },
            {
                "marker_id": "doi:10.1002/advs.202201190",
                "metadata": {
                    "paper_id": "s2:e7cda7beaa70f65e43b46d813b939ffd92b9b0eb",
                    "doi": "10.1002/ADVS.202201190.",
                    "title": "Aerogel-Functionalized Thermoplastic Polyurethane",
                },
            },
        ],
    )
    sections = [
        {
            "section_id": "S01",
            "english_draft_path": str(dp),
            "input_packet_path": str(pp),
            "explanatory_citation_ledger_path": str(ledger),
        }
    ]

    canonical = build_canonical_context(
        _write_manifest(tmp_path, sections), sections
    )

    assert len(canonical["explanatory_papers"]) == 1
    assert "doi:10.1002/advs.202201190" in canonical["explanatory_papers"]
    assert canonical["ref_identity_map"][
        "s2:e7cda7beaa70f65e43b46d813b939ffd92b9b0eb"
    ]["paper_id"] == "doi:10.1002/advs.202201190"
    assert canonical["ref_identity_map"][
        "e7cda7beaa70f65e43b46d813b939ffd92b9b0eb"
    ]["paper_id"] == "doi:10.1002/advs.202201190"


def test_marker_alias_conflict_for_two_different_dois(tmp_path) -> None:
    """One marker pointing at two different DOIs is never merged."""
    draft = "# S01\n\n[REF:shared-marker]\n"
    dp, pp = _write_section(tmp_path, "S01", "One", draft)
    ledger = _write_ledger(
        tmp_path,
        "S01",
        [
            {
                "marker_id": "shared-marker",
                "metadata": {
                    "paper_id": "doi:10.1111/aaa.1111",
                    "doi": "10.1111/aaa.1111",
                },
            },
            {
                "marker_id": "shared-marker",
                "metadata": {
                    "paper_id": "s2:bbb",
                    "doi": "10.2222/bbb.2222",
                },
            },
        ],
    )
    sections = [
        {
            "section_id": "S01",
            "english_draft_path": str(dp),
            "input_packet_path": str(pp),
            "explanatory_citation_ledger_path": str(ledger),
        }
    ]

    with pytest.raises(ManifestError, match="explanatory marker alias conflict"):
        build_canonical_context(
            _write_manifest(tmp_path, sections), sections
        )


class _FlowProvider:
    """Captures per-role payloads; gap critic returns minimal valid output."""

    def __init__(self) -> None:
        self.payloads: dict[str, dict[str, Any]] = {}

    def __call__(self, role: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        self.payloads[role] = dict(payload)
        if role == "evidence_attribution_critic":
            return {
                "section_argument_gaps": [],
                "review_structure_gaps": [],
                "gap_value_decisions": [],
                "citation_audit": [],
                "attribution_issues": [],
                "source_concentration": [],
                "retrieval_gap_proposals": [],
                "visual_evidence_notes": [],
                "retained_advisory_issues": [],
            }
        return DeterministicRoleProvider()(role, payload)


def test_prior_results_flow_into_gap_critic_and_commander(tmp_path) -> None:
    manifest, sections, _ = _three_sections(tmp_path)
    provider = _FlowProvider()
    run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=tmp_path / "out",
        role_provider=provider,
    )
    auditor_payload = provider.payloads.get("coverage_auditor") or {}
    assert "previous_role_results" not in auditor_payload
    critic_payload = provider.payloads.get("evidence_attribution_critic") or {}
    prior = critic_payload.get("previous_role_results") or {}
    assert "structure_strategist" in prior
    assert "scientific_synthesis_editor" in prior
    assert "coverage_auditor" in prior
    assert "evidence_attribution_critic" not in prior
    structure_prior = prior.get("structure_strategist") or {}
    assert structure_prior.get("structure_candidates")
    auditor_prior = prior.get("coverage_auditor") or {}
    assert "coverage_audit_summary" in auditor_prior
    commander_payload = provider.payloads.get("commander_synthesis") or {}
    commander_prior = commander_payload.get("previous_role_results") or {}
    assert "evidence_attribution_critic" in commander_prior
    assert "structure_strategist" in commander_prior
    assert "scientific_synthesis_editor" in commander_prior
    assert "coverage_auditor" in commander_prior
    reviews = json.loads(
        (tmp_path / "out" / "role_reviews.json").read_text(encoding="utf-8")
    )
    auditor_result = reviews["roles"]["coverage_auditor"]["result"]
    assert auditor_result["section_argument_gaps"] == []
    assert auditor_result["review_structure_gaps"] == []
    assert auditor_result["gap_value_decisions"] == []


def test_coverage_auditor_cannot_approve_own_candidates(tmp_path) -> None:
    manifest, sections, _ = _three_sections(tmp_path)
    canonical = build_canonical_context(manifest, sections)
    content = {
        "coverage_audit_summary": "checked all current sections",
        "section_argument_gap_candidates": [
            {
                "gap_type": "section_argument_gap",
                "unique_contribution": "distinct analytical sub-role",
                "why_existing_structure_cannot_absorb": "no current slot",
                "expected_nonduplicate_gain": "absent axis coverage",
                "section_id": "S01",
                "paragraph_ids": ["P01"],
                "success_criterion": "new subsection supplies the role",
                "affected_sections": ["S01"],
                "confidence": "high",
            }
        ],
        "section_argument_gaps": [
            {
                "gap_type": "section_argument_gap",
                "gap_id": "GAP-SEC-001",
                "unique_contribution": "self-approved",
                "why_existing_structure_cannot_absorb": "x",
                "expected_nonduplicate_gain": "y",
                "affected_sections": ["S01"],
                "confidence": "high",
                "existing_coverage": {"paragraph_ids": [], "summary": "s"},
                "residual_gap": "r",
                "decision": "approve",
            }
        ],
        "gap_value_decisions": [
            {"gap_id": "GAP-SEC-001", "decision": "approve"}
        ],
    }
    cleaned, issues, usable = sanitize_role_result(
        "coverage_auditor", content, canonical
    )
    assert usable is True
    assert cleaned["coverage_gap_candidates"][0]["candidate_id"] == (
        "CAND-SEC-001"
    )
    assert cleaned["coverage_gap_candidates"][0]["paragraph_ids"] == [
        "S01-P01"
    ]
    assert cleaned["section_argument_gaps"] == []
    assert cleaned["review_structure_gaps"] == []
    assert cleaned["gap_value_decisions"] == []


def test_gap_critic_rejects_candidate_already_covered_by_current_draft(
    tmp_path,
) -> None:
    manifest, sections, _ = _three_sections(tmp_path)
    draft_path = Path(sections[0]["english_draft_path"])
    draft = draft_path.read_text(encoding="utf-8")
    draft = draft.replace(
        "Opening paragraph of S01 with [REF:paper_S01].",
        "Opening paragraph of S01 with [REF:paper_S01].\n\n"
        "The current draft already supplies a decision framework with "
        "criteria and evaluation steps.",
        1,
    )
    draft_path.write_text(draft, encoding="utf-8")
    packet_path = Path(sections[0]["input_packet_path"])
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    packet["open_questions"] = ["Propose a decision framework subsection."]
    packet["manuscript_context"]["reviewer_comments_retained"] = [
        {"comment": "Add a decision framework subsection."}
    ]
    packet_path.write_text(
        json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def auditor_responder(
        role: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "coverage_audit_summary": "audited current text",
            "section_argument_gap_candidates": [
                {
                    "candidate_id": "CAND-SEC-001",
                    "gap_type": "section_argument_gap",
                    "unique_contribution": "decision framework subsection",
                    "why_existing_structure_cannot_absorb": "no slot",
                    "expected_nonduplicate_gain": "framework clarity",
                    "section_id": "S01",
                    "paragraph_ids": ["S01-P01"],
                    "success_criterion": (
                        "subsection supplies the decision framework"
                    ),
                    "affected_sections": ["S01"],
                    "confidence": "high",
                }
            ],
            "review_structure_gap_candidates": [],
        }

    def critic_responder(
        role: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        section_text = payload["canonical_context"]["sections"][0][
            "draft_text"
        ]
        assert "decision framework" in section_text
        return {
            "section_argument_gaps": [],
            "review_structure_gaps": [],
            "gap_value_decisions": [
                {
                    "gap_id": "CAND-SEC-001",
                    "decision": "reject",
                    "confidence": "high",
                    "reason": (
                        "current draft already supplies the framework"
                    ),
                    "provenance_prior_role": "coverage_auditor",
                }
            ],
            "rejected_gap_candidates": [
                {
                    "candidate_id": "CAND-SEC-001",
                    "gap_type": "section_argument_gap",
                    "reason": "already covered by current paragraph S01-P02",
                    "existing_coverage": {
                        "paragraph_ids": ["S01-P02"],
                        "summary": (
                            "decision framework with criteria is already "
                            "present in the current draft"
                        ),
                    },
                    "residual_gap": "",
                    "provenance_prior_role": "coverage_auditor",
                }
            ],
            "citation_audit": [],
            "attribution_issues": [],
            "source_concentration": [],
            "retrieval_gap_proposals": [],
            "visual_evidence_notes": [],
            "coverage_search_notes": (
                "searched current paragraphs before rejecting"
            ),
            "retained_advisory_issues": [],
        }

    before = _source_hashes(manifest, sections)
    provider = Provider(
        responders={
            "coverage_auditor": auditor_responder,
            "evidence_attribution_critic": critic_responder,
        }
    )
    output = tmp_path / "out"
    run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=provider,
    )
    assert _source_hashes(manifest, sections) == before
    work_order = json.loads(
        (output / "global_commander_work_order.json").read_text(
            encoding="utf-8"
        )
    )
    assert work_order["section_argument_gaps"] == []
    assert work_order["review_structure_gaps"] == []
    assert work_order["gap_value_decisions"][0]["decision"] == "reject"
    assert work_order["rejected_gap_candidates"][0]["candidate_id"] == (
        "CAND-SEC-001"
    )
    assert work_order["rejected_gap_candidates"][0][
        "existing_coverage"
    ]["summary"].startswith("decision framework")


def test_gap_critic_approves_genuinely_absent_subsection(tmp_path) -> None:
    manifest, sections, _ = _three_sections(tmp_path)

    def auditor_responder(
        role: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "coverage_audit_summary": "audited current text",
            "section_argument_gap_candidates": [
                {
                    "candidate_id": "CAND-SEC-001",
                    "gap_type": "section_argument_gap",
                    "unique_contribution": "distinct analytical sub-role",
                    "why_existing_structure_cannot_absorb": (
                        "no current subsection slot"
                    ),
                    "expected_nonduplicate_gain": (
                        "covers an absent analytical axis"
                    ),
                    "section_id": "S01",
                    "paragraph_ids": [],
                    "success_criterion": (
                        "new subsection supplies the distinct role"
                    ),
                    "affected_sections": ["S01"],
                    "confidence": "high",
                }
            ],
            "review_structure_gap_candidates": [],
        }

    def critic_responder(
        role: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "section_argument_gaps": [
                {
                    "gap_id": "GAP-SEC-001",
                    "gap_type": "section_argument_gap",
                    "unique_contribution": "distinct analytical sub-role",
                    "why_existing_structure_cannot_absorb": (
                        "no current subsection slot"
                    ),
                    "expected_nonduplicate_gain": (
                        "covers an absent analytical axis"
                    ),
                    "success_criterion": (
                        "new subsection supplies the distinct role"
                    ),
                    "affected_sections": ["S01"],
                    "confidence": "high",
                    "existing_coverage": {
                        "paragraph_ids": [],
                        "summary": (
                            "checked current paragraphs; role is absent"
                        ),
                    },
                    "residual_gap": (
                        "current text does not supply the distinct role"
                    ),
                    "decision": "approve",
                }
            ],
            "review_structure_gaps": [],
            "gap_value_decisions": [
                {
                    "gap_id": "GAP-SEC-001",
                    "decision": "approve",
                    "confidence": "high",
                    "reason": "distinct and absent from current text",
                    "provenance_prior_role": "coverage_auditor",
                }
            ],
            "rejected_gap_candidates": [],
            "citation_audit": [],
            "attribution_issues": [],
            "source_concentration": [],
            "retrieval_gap_proposals": [],
            "visual_evidence_notes": [],
            "retained_advisory_issues": [],
        }

    provider = Provider(
        responders={
            "coverage_auditor": auditor_responder,
            "evidence_attribution_critic": critic_responder,
        }
    )
    output = tmp_path / "out"
    run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=provider,
    )
    work_order = json.loads(
        (output / "global_commander_work_order.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(work_order["section_argument_gaps"]) == 1
    approved = work_order["section_argument_gaps"][0]
    assert approved["gap_id"] == "GAP-SEC-001"
    assert approved["existing_coverage"]["paragraph_ids"] == []
    assert "distinct role" in approved["residual_gap"]
    assert work_order["review_structure_gaps"] == []
    assert work_order["gap_value_decisions"][0]["decision"] == "approve"


def test_structure_strategist_one_candidate_recovery(tmp_path) -> None:
    manifest, sections, _ = _three_sections(tmp_path)
    section_ids = [section["section_id"] for section in sections]
    calls = {"count": 0}

    def responder(
        role: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        calls["count"] += 1
        if payload.get("mode") == "expand_candidates":
            assert len(payload["current_candidates"]) == 1
            assert payload["requested_total"] == 2
            return {
                "structure_candidates": [
                    {
                        "story_shape": "synthesis_first_reversal",
                        "narrative_backbone": (
                            "Reverse the reading path."
                        ),
                        "section_order": list(reversed(section_ids)),
                        "reader_path": (
                            "Start from synthesis and backtrack."
                        ),
                        "rationale": "Alternative shape for comparison.",
                        "risks": [],
                    }
                ]
            }
        return {
            "structure_candidates": [
                {
                    "story_shape": "manifest_order",
                    "narrative_backbone": "Keep manifest order.",
                    "section_order": list(section_ids),
                    "reader_path": "Follow manifest order.",
                    "rationale": "Primary shape.",
                    "risks": [],
                }
            ]
        }

    provider = Provider(
        responders={"structure_strategist": responder}
    )
    output = tmp_path / "out"
    summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=provider,
    )
    assert summary["status"] == "completed"
    assert calls["count"] == 2
    reviews = json.loads(
        (output / "role_reviews.json").read_text(encoding="utf-8")
    )
    record = reviews["roles"]["structure_strategist"]
    assert record["repair_used"] is True
    candidates = record["result"]["structure_candidates"]
    assert len(candidates) == 2
    assert {item["story_shape"] for item in candidates} == {
        "manifest_order",
        "synthesis_first_reversal",
    }
    assert summary["repair_calls"]["structure_strategist"] is True


def test_structure_strategist_expansion_failure_fails_open(tmp_path) -> None:
    manifest, sections, _ = _three_sections(tmp_path)
    section_ids = [section["section_id"] for section in sections]
    calls = {"count": 0}

    def responder(
        role: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        calls["count"] += 1
        if payload.get("mode") == "expand_candidates":
            raise RuntimeError("expansion provider unavailable")
        return {
            "structure_candidates": [
                {
                    "story_shape": "manifest_order",
                    "narrative_backbone": "Keep manifest order.",
                    "section_order": list(section_ids),
                    "reader_path": "Follow manifest order.",
                    "rationale": "Primary shape.",
                    "risks": [],
                }
            ]
        }

    provider = Provider(
        responders={"structure_strategist": responder}
    )
    output = tmp_path / "out"
    summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=provider,
    )
    assert summary["status"] == "completed"
    assert calls["count"] == 2
    reviews = json.loads(
        (output / "role_reviews.json").read_text(encoding="utf-8")
    )
    record = reviews["roles"]["structure_strategist"]
    assert record["status"] == "completed"
    assert record["repair_used"] is True
    assert len(record["result"]["structure_candidates"]) == 1
    assert any(
        "candidate expansion failed" in issue
        for issue in record["validation_issues"]
    )


def test_work_order_retains_all_strategist_candidates(tmp_path) -> None:
    manifest, sections, _ = _three_sections(tmp_path)

    def commander_responder(
        role: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        base = DeterministicRoleProvider()(role, payload)
        base["structure_candidates"] = []
        return base

    provider = Provider(
        responders={"commander_synthesis": commander_responder}
    )
    output = tmp_path / "out"
    run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=provider,
    )
    work_order = json.loads(
        (output / "global_commander_work_order.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(work_order["structure_candidates"]) == 2
    assert {item["story_shape"] for item in work_order["structure_candidates"]} == {
        "manifest_order",
        "synthesis_first_reversal",
    }


def test_commander_section_decisions_locally_completed(tmp_path) -> None:
    manifest, sections, _ = _three_sections(tmp_path)

    def commander_responder(
        role: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        base = DeterministicRoleProvider()(role, payload)
        base["section_decisions"] = [
            {
                "section_id": "S01",
                "decision": "retain",
                "rationale": "explicit commander decision",
            },
            {
                "section_id": "S02",
                "decision": "retain",
                "rationale": "explicit commander decision",
            },
        ]
        return base

    provider = Provider(
        responders={"commander_synthesis": commander_responder}
    )
    output = tmp_path / "out"
    summary = run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=output,
        role_provider=provider,
    )
    assert summary["status"] == "completed"
    work_order = json.loads(
        (output / "global_commander_work_order.json").read_text(
            encoding="utf-8"
        )
    )
    decisions = {
        item["section_id"]: item
        for item in work_order["section_decisions"]
    }
    assert set(decisions) == {"S01", "S02", "S03"}
    assert decisions["S01"]["provenance"] == ""
    assert decisions["S03"]["decision"] == "retain"
    assert decisions["S03"]["provenance"] == "local_completion"
    assert decisions["S03"]["responsibility"] == (
        "Argument role for S03"
    )


def test_two_gap_allowlist_required_fields_and_deterministic_ids(
    tmp_path,
) -> None:
    manifest, sections, _ = _three_sections(tmp_path)
    canonical = build_canonical_context(manifest, sections)
    content = {
        "section_argument_gaps": [
            {
                "gap_type": "section_argument_gap",
                "unique_contribution": "distinct new claim role",
                "why_existing_structure_cannot_absorb": "no slot",
                "expected_nonduplicate_gain": "new evidence axis",
                "missing_claim_roles": ["boundary_condition"],
                "fact_units": ["unit-x"],
                "required_material_strength": {"minimum": "qualified"},
                "success_criterion": "quote bound to new role",
                "one_round_stop_reason": "one wave only",
                "affected_sections": ["S01"],
                "confidence": "high",
                "existing_coverage": {
                    "paragraph_ids": ["S01-P01"],
                    "summary": "No current paragraph supplies this role.",
                },
                "residual_gap": (
                    "The existing paragraphs stop short of the success "
                    "criterion."
                ),
            },
            {
                "gap_type": "section_argument_gap",
                "unique_contribution": "missing confidence",
                "why_existing_structure_cannot_absorb": "x",
                "expected_nonduplicate_gain": "y",
                "affected_sections": ["S02"],
            },
            {
                "gap_type": "review_structure_gap",
                "unique_contribution": "wrong slot",
                "why_existing_structure_cannot_absorb": "x",
                "expected_nonduplicate_gain": "y",
                "affected_sections": [],
                "confidence": "low",
            },
        ],
        "review_structure_gaps": [
            {
                "gap_type": "review_structure_gap",
                "unique_contribution": "new chapter axis",
                "why_existing_structure_cannot_absorb": "no chapter fits",
                "expected_nonduplicate_gain": "axis coverage",
                "affected_sections": [],
                "closure_criterion": "chapter drafted and reviewed",
                "confidence": "medium",
                "existing_coverage": {
                    "paragraph_ids": [],
                    "summary": "Checked all current chapters; axis is absent.",
                },
                "residual_gap": (
                    "No existing chapter satisfies the success criterion."
                ),
            }
        ],
        "gap_value_decisions": [
            {
                "gap_id": "GAP-SEC-001",
                "decision": "approve",
                "confidence": "high",
                "reason": "distinct value",
                "provenance_prior_role": "structure_strategist",
            },
            {"gap_id": "", "decision": "approve"},
        ],
        "rejected_gap_candidates": [
            {
                "candidate_id": "CAND-SEC-002",
                "gap_type": "section_argument_gap",
                "reason": "Already covered by the current draft.",
                "existing_coverage": {
                    "paragraph_ids": ["S01-P01"],
                    "summary": "Current draft already supplies it.",
                },
                "provenance_prior_role": "coverage_auditor",
            }
        ],
        "structure_candidates": [
            {
                "story_shape": "shape",
                "narrative_backbone": "backbone",
                "section_order": ["S01", "S02", "S03"],
                "reader_path": "path",
                "rationale": "reason",
                "risks": ["risk"],
            }
        ],
        "selected_story_shape": {
            "candidate_id": "STRUCT-001",
            "story_shape": "shape",
            "rationale": "reason",
            "provenance_prior_role": "structure_strategist",
        },
        "reader_path_findings": [
            {
                "section_id": "S01",
                "assessment": "reader may lose thread",
                "recommendation": "add transition",
                "severity": "advisory",
            }
        ],
    }
    cleaned, issues, usable = sanitize_role_result(
        "evidence_attribution_critic", content, canonical
    )
    assert usable is True
    gaps = cleaned["section_argument_gaps"]
    assert len(gaps) == 1
    assert gaps[0]["gap_id"] == "GAP-SEC-001"
    assert gaps[0]["unique_contribution"] == "distinct new claim role"
    assert gaps[0]["missing_claim_roles"] == ["boundary_condition"]
    assert gaps[0]["one_round_stop_reason"] == "one wave only"
    assert gaps[0]["existing_coverage"]["summary"] == (
        "No current paragraph supplies this role."
    )
    assert "stop short" in gaps[0]["residual_gap"]
    assert cleaned["review_structure_gaps"][0]["gap_id"] == "GAP-REV-001"
    assert cleaned["review_structure_gaps"][0][
        "one_round_stop_reason"
    ] == "chapter drafted and reviewed"
    assert cleaned["review_structure_gaps"][0][
        "existing_coverage"
    ]["paragraph_ids"] == []
    assert cleaned["gap_value_decisions"][0]["decision_id"] == "GAPVAL-001"
    assert cleaned["gap_value_decisions"][0]["gap_id"] == "GAP-SEC-001"
    assert cleaned["structure_candidates"][0]["candidate_id"] == "STRUCT-001"
    assert cleaned["reader_path_findings"][0]["finding_id"] == "READER-001"
    assert cleaned["selected_story_shape"]["candidate_id"] == "STRUCT-001"
    assert cleaned["rejected_gap_candidates"][0]["rejection_id"] == "REJECT-001"
    assert cleaned["rejected_gap_candidates"][0]["candidate_id"] == (
        "CAND-SEC-002"
    )
    assert cleaned["rejected_gap_candidates"][0]["decision"] == "reject"
    assert any("missing required fields" in issue for issue in issues)
    assert any("gap_type mismatch" in issue for issue in issues)

    cleaned_again, _, _ = sanitize_role_result(
        "evidence_attribution_critic", content, canonical
    )
    assert cleaned_again["section_argument_gaps"] == gaps
    assert cleaned_again["gap_value_decisions"] == cleaned[
        "gap_value_decisions"
    ]


def test_empty_gaps_allowed_and_evidence_court_fields_rejected(
    tmp_path,
) -> None:
    manifest, sections, _ = _three_sections(tmp_path)
    canonical = build_canonical_context(manifest, sections)
    empty, issues_empty, usable_empty = sanitize_role_result(
        "evidence_attribution_critic",
        {
            "section_argument_gaps": [],
            "review_structure_gaps": [],
            "gap_value_decisions": [],
        },
        canonical,
    )
    assert usable_empty is True
    assert empty["section_argument_gaps"] == []
    assert empty["review_structure_gaps"] == []
    assert empty["gap_value_decisions"] == []

    bad, issues_bad, usable_bad = sanitize_role_result(
        "evidence_attribution_critic",
        {"claim_evidence_gap": [{"claim_id": "S01-CL01"}]},
        canonical,
    )
    assert usable_bad is False
    assert any("forbidden evidence-court" in issue for issue in issues_bad)


def test_additive_work_order_fields_in_dry_run(tmp_path) -> None:
    manifest, sections, _ = _three_sections(tmp_path)
    run_global_manuscript_commander(
        manifest_path=manifest,
        output_dir=tmp_path / "out",
        role_provider=Provider(),
    )
    work_order = json.loads(
        (tmp_path / "out" / "global_commander_work_order.json").read_text(
            encoding="utf-8"
        )
    )
    for key in (
        "structure_candidates",
        "selected_story_shape",
        "reader_path_findings",
        "section_argument_gaps",
        "review_structure_gaps",
        "gap_value_decisions",
        "rejected_gap_candidates",
        "coverage_audit_summary",
    ):
        assert key in work_order
    assert work_order["proposed_patch_set"] == []
    assert work_order["read_only_declaration"]["chapter_text_changed"] is False
