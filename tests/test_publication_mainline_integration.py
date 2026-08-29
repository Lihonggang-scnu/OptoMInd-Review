"""Deterministic zero-network integration tests for the publication mainline."""

from __future__ import annotations

import json
import inspect
import re
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping

import pytest

import optomind_research.chapter_asset_enhancer as chapter_enhancer
from optomind_research.runtime.global_manuscript_commander import (
    DeterministicRoleProvider,
)
from optomind_research.runtime.publication_mainline_adapter import (
    ENHANCEMENT_REUSE_STATE_JSON,
    _LocalMetadataCallback,
    build_enhancer_input_packet,
    run_publication_mainline,
)


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory for this suite."""

    root = (
        Path(__file__).resolve().parents[1]
        / ".pytest-basetemp-publication-mainline"
    )
    root.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)[:40]
    path = root / f"{safe_name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


class _CommanderProvider:
    """Offline provider that records calls and delegates to dry-mode outputs."""

    def __init__(self) -> None:
        self.delegate = DeterministicRoleProvider()
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    def __call__(self, role: str, payload: Mapping[str, Any]) -> Any:
        self.calls.append((role, dict(payload)))
        return self.delegate(role, payload)


def _write_r4_section(
    root: Path,
    section_id: str,
    title: str,
) -> dict[str, Any]:
    """Create a minimal R4 authoring section with a legacy pre-enhancement draft."""

    section_dir = root / "authoring" / "sections" / section_id
    section_dir.mkdir(parents=True, exist_ok=True)
    (section_dir / "SECTION_DRAFT_EN.md").write_text(
        f"Legacy pre-enhancement draft for {section_id}.",
        encoding="utf-8",
    )
    claim_id = f"{section_id}-C1"
    chunk_id = f"chunk-{section_id}"
    paper_id = f"paper-{section_id}"
    context = {
        "schema_version": "3.2",
        "section_id": section_id,
        "section_title": title,
        "chapter_argument": f"Argument for {section_id}.",
        "scope_guardrails": [f"Scope {section_id}"],
        "coverage_status": "complete",
        "total_sources": 1,
        "sources_by_role": {"direct": 1},
        "sources": [
            {
                "paper_id": paper_id,
                "doi": f"10.1000/{section_id}",
                "title": f"Paper {section_id}",
                "literature_role": "direct",
                "scope_fit": "in_domain",
                "use_permission": "factual_support",
            }
        ],
        "claims": [
            {
                "claim_id": claim_id,
                "effective_statement": (
                    f"The reviewed work supports the {section_id} claim."
                ),
                "writing_permission": "factual_assertion",
                "evidence_binding_status": "direct",
                "claim_state": "ready_for_write",
                "supported_components": [],
                "missing_evidence_components": [],
                "caveats": [],
            }
        ],
        "full_review_argument": "A three-section review of the question.",
        "section_role": "body",
        "transition_contract": {},
        "section_contract": {
            "title": title,
            "central_thesis": f"Central thesis {section_id}.",
            "argument_role": "body",
        },
    }
    (section_dir / "SECTION_AUTHORING_CONTEXT.json").write_text(
        json.dumps(context, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    plan = {
        "schema_version": "3.0",
        "section_id": section_id,
        "paragraphs": [
            {
                "paragraph_index": 0,
                "function": "mechanism",
                "topic_sentence": f"Opening {section_id}.",
                "key_claims": [claim_id],
                "evidence_chunk_ids": [chunk_id],
                "paper_ids": [paper_id],
                "writing_permission": "factual_assertion",
            }
        ],
        "open_questions": [],
    }
    (section_dir / "SECTION_ARGUMENT_PLAN.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    evidence = {
        "schema_version": "3.0",
        "section_id": section_id,
        "items": [
            {
                "chunk_id": chunk_id,
                "paper_id": paper_id,
                "paper_title": f"Paper {section_id}",
                "claim_ids": [claim_id],
                "exact_spans": [f"Evidence span for {section_id}."],
                "literature_role": "direct",
                "scope_fit": "in_domain",
                "evidence_level": "fulltext",
                "writing_permission": "factual_assertion",
            }
        ],
        "uncovered_claim_ids": [],
    }
    (section_dir / "SECTION_EVIDENCE_PACKET.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "section_id": section_id,
        "title": title,
        "argument_role": "body",
        "key_questions": [],
    }


def _write_phase3_authoring_section(
    root: Path,
    section_id: str = "S01",
) -> Path:
    section_dir = root / "authoring" / "sections" / section_id
    section_dir.mkdir(parents=True, exist_ok=True)
    (section_dir / "SECTION_DRAFT_EN.md").write_text(
        f"Legacy {section_id} draft.",
        encoding="utf-8",
    )
    (section_dir / "SECTION_AUTHORING_CONTEXT.json").write_text(
        json.dumps(
            {
                "schema_version": "3.2",
                "section_id": section_id,
                "section_title": "Phase3 Section",
                "chapter_argument": "Phase3 argument.",
                "section_role": "body",
                "section_contract": {
                    "title": "Phase3 Section",
                    "central_thesis": "Phase3 thesis.",
                    "argument_role": "body",
                },
                "claims": [],
            }
        ),
        encoding="utf-8",
    )
    (section_dir / "SECTION_EVIDENCE_PACKET.json").write_text(
        json.dumps(
            {
                "schema_version": "3.0",
                "section_id": section_id,
                "items": [],
                "uncovered_claim_ids": [],
            }
        ),
        encoding="utf-8",
    )
    phase3_root = root / "phase3_argument_orchestration"
    ledger_path = (
        phase3_root
        / "coverage_snapshot"
        / "sections"
        / section_id
        / "SECTION_SOURCE_LEDGER.json"
    )
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps(
            {
                "schema_version": "research_harness.phase3_section_source_ledger.v1",
                "section_id": section_id,
                "sources": [
                    {
                        "paper_id": "paper-p1",
                        "title": "Paper One",
                        "content_depth": "structured_snippet",
                        "materialization_route": "s2_structured_body_snippet",
                        "scope_fit": "direct",
                        "canonical_chunk_ids": ["chunk-p1"],
                    },
                    {
                        "paper_id": "paper-p2",
                        "title": "Paper Two",
                        "content_depth": "fulltext",
                        "materialization_route": "m3_oa_fulltext_parse",
                        "scope_fit": "direct",
                        "canonical_chunk_ids": ["chunk-p2"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    claim_graph_path = phase3_root / "CLAIM_GRAPH.json"
    claim_graph = {
        "nodes": [
            {
                "claim_id": "C1",
                "evidence_spans": [
                    {
                        "chunk_id": "chunk-p1",
                        "quote": "Verified exact quote from Paper One.",
                        "quote_verified": True,
                        "quote_match_mode": "normalized_exact",
                        "source_locator": {
                            "provider": "semantic_scholar",
                            "chunk_id": "chunk-p1",
                            "paper_id": "paper-p1",
                        },
                        "content_depth": "structured_snippet",
                    }
                ],
            }
        ]
    }
    claim_graph_path.write_text(
        json.dumps(claim_graph, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    phase3_path = section_dir / "PHASE3_AUTHORING_CONTEXT.json"
    phase3_path.write_text(
        json.dumps(
            {
                "schema_version": "r4.phase3_authoring_contract.v2",
                "section_id": section_id,
                "authorable_claim_ids": ["C1"],
                "excluded_claim_ids": [],
                "claims": [
                    {
                        "claim_id": "C1",
                        "statement": "A bound Phase3 claim.",
                        "writing_permission": "factual_assertion",
                        "evidence_binding_status": "direct",
                        "permission_status": "bound",
                        "claim_state": "grounded",
                        "core_chunk_ids": ["chunk-p1"],
                        "core_paper_ids": ["paper-p1"],
                        "supporting_text_chunk_ids": ["chunk-p1"],
                        "missing_evidence_components": [],
                    }
                ],
                "source_ledger_path": str(ledger_path),
                "artifact_refs": {
                    "CLAIM_GRAPH.json": {
                        "path": "CLAIM_GRAPH.json",
                        "sha256": "",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return phase3_path


def _write_exact_chunk_db(
    root: Path,
    *,
    chunk_id: str,
    paper_id: str,
    text: str,
) -> Path:
    path = root / "exact_chunks.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE text_chunks (
            chunk_id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL,
            text TEXT NOT NULL,
            evidence_level TEXT,
            source_kind TEXT,
            provenance_json TEXT,
            content_depth TEXT,
            use_permission TEXT,
            context_complete INTEGER,
            scope_fit TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO text_chunks(
            chunk_id, paper_id, text, evidence_level, source_kind,
            provenance_json, content_depth, use_permission,
            context_complete, scope_fit
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            chunk_id,
            paper_id,
            text,
            "fulltext",
            "fulltext",
            json.dumps({"bound": "exact_chunk_id"}),
            "fulltext",
            "factual_support",
            1,
            "in_domain",
        ),
    )
    conn.commit()
    conn.close()
    return path


def _write_required_enhanced_artifacts(
    output_dir: Path,
    *,
    section_id: str,
    title: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    first = f"Enhanced {section_id} first paragraph with [REF:paper-{section_id}]."
    second = f"Enhanced {section_id} second paragraph with [REF:s2:{section_id}-bg]."
    full_text = f"# {title}\n\n{first}\n\n{second}"
    (output_dir / "ENHANCED_CHAPTER.md").write_text(full_text, encoding="utf-8")
    (output_dir / "CHAPTER_ARGUMENT_PLAN.json").write_text(
        json.dumps(
            {
                "schema_version": "chapter_asset_enhancer.v1",
                "section_id": section_id,
                "plan": {
                    "title": title,
                    "chapter_thesis": f"Thesis {section_id}.",
                    "reader_takeaway": f"Takeaway {section_id}.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                },
                "warnings": [],
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "CLAIM_TO_PARAGRAPH_MAP.json").write_text(
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
    (output_dir / "EXPLANATION_BLOCKS.json").write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "blocks": [
                    {
                        "block_index": 1,
                        "title": "First",
                        "prose": first,
                        "goal": "first",
                        "markers": [f"paper-{section_id}"],
                        "claim_handles": [],
                        "evidence_handles": [],
                    },
                    {
                        "block_index": 2,
                        "title": "Second",
                        "prose": second,
                        "goal": "second",
                        "markers": [f"s2:{section_id}-bg"],
                        "claim_handles": [],
                        "evidence_handles": [],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "EXPLANATORY_CITATION_LEDGER.json").write_text(
        json.dumps(
            {
                "schema_version": "chapter_asset_enhancer.v1",
                "section_id": section_id,
                "records": [
                    {
                        "handle": "X01",
                        "marker_id": f"s2:{section_id}-bg",
                        "role": "explanatory_context",
                        "permission": "background_explanation_only",
                        "metadata": {
                            "paper_id": f"s2:{section_id}-bg",
                            "doi": f"10.2000/{section_id}-bg",
                            "title": f"Background {section_id}",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "ENHANCEMENT_REPORT.json").write_text(
        json.dumps(
            {
                "schema_version": "chapter_asset_enhancer.v1",
                "section_id": section_id,
                "status": "enhanced",
                "word_counts": {"enhanced": len(full_text.split())},
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "BLOCK_SCIENTIFIC_REVIEW.json").write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "section_id": section_id,
                "attempted": True,
                "available": True,
                "blocking_count": 0,
                "advisory_count": 0,
                "comments": [],
            }
        ),
        encoding="utf-8",
    )


def _make_fake_enhancement_runner(
    *,
    fail_sections: set[str] | None = None,
    record_report_usage: bool = False,
):
    fail_sections = set(fail_sections or set())
    model_usage = {
        "call_count": 1,
        "total_estimated_cost_cny": 0.05,
        "input_tokens": 10,
        "output_tokens": 20,
    }

    def runner(
        packet_path: Path,
        old_draft_path: Path,
        output_dir: Path,
        live: bool = False,
        **_: Any,
    ) -> dict[str, Any]:
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        section_id = str(packet["section_id"])
        title = str(packet["section_contract"]["title"])
        if section_id in fail_sections:
            raise RuntimeError(f"injected enhancement failure: {section_id}")
        _write_required_enhanced_artifacts(
            output_dir,
            section_id=section_id,
            title=title,
        )
        if record_report_usage:
            report_path = output_dir / "ENHANCEMENT_REPORT.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["model_usage"] = dict(model_usage)
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return {
            "mode": "live",
            "status": "enhanced",
            "section_id": section_id,
            "model_usage": dict(model_usage),
        }

    return runner


def _make_editorial_provider(
    *,
    revised_text: str = "A revised transition with more words.",
    empty_rewrite: bool = False,
    rejected_unsafe: bool = False,
):
    def provider(stage_input: Mapping[str, Any]) -> dict[str, Any]:
        inputs = dict(stage_input.get("inputs") or {})
        sections = [
            section
            for section in inputs.get("sections") or []
            if isinstance(section, Mapping)
        ]
        assembled: list[dict[str, Any]] = []
        full_parts: list[str] = []
        for section in sections:
            text = str(section.get("full_text") or "")
            assembled.append(
                {
                    "kind": "section",
                    "target_id": str(section.get("section_id") or ""),
                    "heading": str(section.get("section_title") or ""),
                    "title": str(section.get("section_title") or ""),
                    "full_text_authority": True,
                    "text": text,
                }
            )
            if text.strip():
                full_parts.append(text)
        original = "old transition"
        revised = original if empty_rewrite else revised_text
        record = {
            "work_item_id": "ER-001",
            "target_block_id": "S01-B001",
            "editorial_kind": "transition",
            "original_text": original,
            "revised_text": revised,
            "original_sha256": "orig-hash",
            "revised_sha256": "same-hash" if empty_rewrite else "rev-hash",
            "status": "accepted",
            "reason": "",
            "author_usage": {"input_tokens": 2, "output_tokens": 3},
            "verifier_usage": {"input_tokens": 1, "output_tokens": 1},
        }
        records = [record]
        accepted_count = 1
        rejected_count = 0
        if rejected_unsafe:
            records.append(
                {
                    "work_item_id": "ER-002",
                    "target_block_id": "S01-B002",
                    "editorial_kind": "transition",
                    "original_text": "second old transition",
                    "revised_text": "second unsafe rewrite",
                    "original_sha256": "orig2-hash",
                    "revised_sha256": "unsafe-hash",
                    "status": "rejected",
                    "reason": "verifier_rejected:unsafe",
                    "author_usage": {
                        "input_tokens": 2,
                        "output_tokens": 2,
                    },
                    "verifier_usage": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                    },
                }
            )
            rejected_count = 1
        return {
            "manuscript": {
                "assembled": assembled,
                "full_text": "\n\n".join(full_parts),
            },
            "audit": {
                "work_item_count": len(records),
                "accepted_count": accepted_count,
                "rejected_count": rejected_count,
                "blocking_unresolved": [],
                "records": records,
            },
            "status": "revised",
            "_usage": {
                "total_input_tokens": 3 + (3 if rejected_unsafe else 0),
                "total_output_tokens": 4 + (3 if rejected_unsafe else 0),
            },
        }

    return provider


def _make_pure_editorial_patch_provider():
    calls: list[Mapping[str, Any]] = []

    def provider(stage_input: Mapping[str, Any]) -> dict[str, Any]:
        calls.append(stage_input)
        return {
            "proposals": [
                {
                    "patch_id": "P-1",
                    "operation": "rewrite_transition",
                    "target": "S01-B001",
                    "rationale": "smooth rhetoric only",
                    "claim_text_change": False,
                    "evidence_change": False,
                },
                {
                    "patch_id": "P-2",
                    "operation": "rewrite_transition",
                    "target": "S01-B002",
                    "rationale": "smooth rhetoric only",
                    "claim_text_change": False,
                    "evidence_change": False,
                },
            ],
            "approval_required": False,
        }

    return provider, calls


def _run_fixture(
    tmp_path: Path,
    *,
    fail_sections: set[str] | None = None,
    missing_draft_sections: set[str] | None = None,
    editorial_provider=None,
    patch_provider=None,
    staged_resume: bool = False,
) -> tuple[Path, Path, Any]:
    project_root = tmp_path / "project"
    authoring_root = project_root / "authoring"
    authoring_root.mkdir(parents=True, exist_ok=True)
    blueprint_sections = [
        _write_r4_section(project_root, "S01", "First Chapter"),
        _write_r4_section(project_root, "S02", "Second Chapter"),
        _write_r4_section(project_root, "S03", "Third Chapter"),
    ]
    blueprint = {
        "schema_version": "research_harness.review_blueprint.v1",
        "sections": blueprint_sections,
        "full_review_argument": "A three-section review of the question.",
        "input_context": {"user_question": "Three-section question."},
    }
    for section_id in missing_draft_sections or set():
        (
            authoring_root / "sections" / section_id / "SECTION_DRAFT_EN.md"
        ).unlink()
    staged_providers: dict[str, Any] = {}
    if patch_provider is not None:
        staged_providers["bounded_patch_proposals"] = patch_provider
    if editorial_provider is not None:
        staged_providers["editorial_revision"] = editorial_provider
    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=project_root / "publication_mainline",
        admitted_section_ids=["S01", "S02", "S03"],
        blueprint=blueprint,
        run_id="integration-run",
        enhancement_live=True,
        enhancement_runner=_make_fake_enhancement_runner(
            fail_sections=fail_sections
        ),
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
        staged_providers=staged_providers,
        staged_resume=staged_resume,
    )
    return project_root, authoring_root, result


def _single_section_fixture(
    tmp_path: Path,
    label: str,
) -> tuple[Path, Path, dict[str, Any]]:
    project_root = tmp_path / label
    authoring_root = project_root / "authoring"
    authoring_root.mkdir(parents=True)
    section = _write_r4_section(project_root, "S01", "Reuse Section")
    blueprint = {
        "schema_version": "research_harness.review_blueprint.v1",
        "sections": [section],
        "full_review_argument": "A one-section review.",
        "input_context": {"user_question": "One-section question."},
    }
    return project_root, authoring_root, blueprint


def test_three_section_mainline_reaches_staged_final_and_downstream(
    tmp_path: Path,
) -> None:
    project_root, authoring_root, result = _run_fixture(
        tmp_path,
        editorial_provider=_make_editorial_provider(),
    )

    assert result.status == "completed"
    assert result.enhanced_sections == ["S01", "S02", "S03"]
    assert result.failed_sections == []
    assert result.final_review_path is not None
    assert result.final_review_path.is_file()
    assert result.final_review_path.parent.name == "staged_completion"
    assert result.final_review_path.name == "STAGED_COMPLETE_REVIEW_EN.md"
    assert "Enhanced S01 first paragraph" in result.final_review_path.read_text(
        encoding="utf-8"
    )
    assert "Legacy pre-enhancement" not in result.final_review_path.read_text(
        encoding="utf-8"
    )

    assert result.downstream_review_work_dir is not None
    for section_id in ("S01", "S02", "S03"):
        downstream_draft = (
            result.downstream_review_work_dir
            / section_id
            / "SECTION_DRAFT_EN.md"
        )
        assert downstream_draft.is_file()
        assert "Enhanced" in downstream_draft.read_text(encoding="utf-8")
        assert "Legacy pre-enhancement" not in downstream_draft.read_text(
            encoding="utf-8"
        )

    assert result.handoff_path is not None
    assert result.commander_work_order_path is not None
    assert result.commander_work_order_path.is_file()
    assert result.staged_state is not None
    assert result.staged_state.status == "completed"
    assert result.editorial_closure_completed is True
    assert result.accounting["accepted_applied_count"] == 1
    enhancement_metrics = result.stage_metrics[
        "publication_mainline_enhancement"
    ]
    assert enhancement_metrics["cost_cny"] == pytest.approx(0.15)
    assert enhancement_metrics["cost_accounting"] == "provider_priced"
    assert (
        result.stage_metrics["publication_mainline_staged_completion"][
            "cost_accounting"
        ]
        == "estimated_from_tokens"
    )
    assert (
        result.stage_metrics["publication_mainline_staged_completion"][
            "cost_cny"
        ]
        > 0
    )
    assert "publication_mainline_staged_completion" not in (
        result.summary["unaccounted_cost_stages"]
    )


def test_empty_rewrite_is_not_reported_as_successful_applied_revision(
    tmp_path: Path,
) -> None:
    _, _, result = _run_fixture(
        tmp_path,
        editorial_provider=_make_editorial_provider(empty_rewrite=True),
    )

    assert result.final_review_path is not None
    assert result.status == "partial"
    assert result.editorial_closure_completed is False
    assert result.accounting["accepted_applied_count"] == 0
    assert result.accounting["no_change_count"] == 1
    assert "editorial/quality closure is not completed" in (
        result.fail_open_issues
    )


def test_pure_editorial_patches_with_rejected_unsafe_complete_and_resume(
    tmp_path: Path,
) -> None:
    patch_provider, patch_calls = _make_pure_editorial_patch_provider()
    editorial_provider = _make_editorial_provider(rejected_unsafe=True)
    first = _run_fixture(
        tmp_path,
        editorial_provider=editorial_provider,
        patch_provider=patch_provider,
    )[2]

    assert first.status == "completed"
    assert first.editorial_closure_completed is True
    assert first.accounting["accepted_applied_count"] == 1
    assert first.accounting["rejected_unsafe_count"] == 1
    assert first.accounting["blocking_unresolved"] == []
    assert first.fail_open_issues == []
    assert first.staged_state is not None
    assert first.staged_state.status == "completed"
    assert first.staged_state.awaiting_approval_stages == []
    assert (
        first.staged_state.stages["bounded_patch_proposals"].approval_required
        is False
    )
    staged_metric = first.stage_metrics[
        "publication_mainline_staged_completion"
    ]
    assert staged_metric["cost_cny"] > 0
    assert staged_metric["cost_accounting"] == "estimated_from_tokens"

    patch_calls.clear()
    second = _run_fixture(
        tmp_path,
        editorial_provider=editorial_provider,
        patch_provider=patch_provider,
        staged_resume=True,
    )[2]
    assert second.status == "completed"
    assert second.editorial_closure_completed is True
    assert second.accounting["rejected_unsafe_count"] == 1
    assert patch_calls == []
    assert (
        second.staged_state.stages["bounded_patch_proposals"].status
        == "noop"
    )


def test_mainline_resume_releases_stale_awaiting_pure_editorial_artifacts(
    tmp_path: Path,
) -> None:
    patch_provider, _ = _make_pure_editorial_patch_provider()
    project_root, _, first = _run_fixture(
        tmp_path,
        editorial_provider=_make_editorial_provider(rejected_unsafe=True),
        patch_provider=patch_provider,
    )
    assert first.status == "completed"

    # Re-create the pre-fix artifact state: the pure-editorial patch stage was
    # persisted as awaiting approval even though its payload needs none.
    state_path = (
        project_root
        / "publication_mainline"
        / "staged_completion"
        / "staged_article_completion_state.json"
    )
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

    resumed = _run_fixture(
        tmp_path,
        editorial_provider=_make_editorial_provider(rejected_unsafe=True),
        patch_provider=patch_provider,
        staged_resume=True,
    )[2]
    assert resumed.status == "completed"
    assert resumed.editorial_closure_completed is True
    assert resumed.staged_state.status == "completed"
    assert resumed.staged_state.awaiting_approval_stages == []
    patch_stage = resumed.staged_state.stages["bounded_patch_proposals"]
    assert patch_stage.status == "noop"
    assert patch_stage.approval_required is False
    assert resumed.final_review_path is not None
    assert resumed.final_review_path.is_file()


def test_enhancement_reuse_includes_persisted_recorded_cost(
    tmp_path: Path,
) -> None:
    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "reuse-recorded-cost"
    )
    runner = _make_fake_enhancement_runner(record_report_usage=True)
    common = {
        "project_root": project_root,
        "authoring_work_dir": authoring_root,
        "output_root": project_root / "publication_mainline",
        "admitted_section_ids": ["S01"],
        "blueprint": blueprint,
        "run_id": "reuse-recorded-cost-run",
        "enhancement_live": True,
        "enhancement_runner": runner,
        "commander_live": False,
        "commander_role_provider": _CommanderProvider(),
        "staged_live": False,
    }
    first = run_publication_mainline(**common)
    assert first.status == "completed"
    assert (
        first.stage_metrics["publication_mainline_enhancement"]["sections"][
            "S01"
        ]["cost_cny"]
        == pytest.approx(0.05)
    )

    second = run_publication_mainline(**common)
    section_metrics = second.stage_metrics[
        "publication_mainline_enhancement"
    ]["sections"]["S01"]
    assert section_metrics["reused"] is True
    assert section_metrics["fingerprint_matched"] is True
    assert section_metrics["cost_cny"] == pytest.approx(0.05)
    assert section_metrics["input_tokens"] == 10
    assert section_metrics["output_tokens"] == 20
    assert section_metrics["cost_accounting"] == "provider_priced"
    assert second.cost_cny == pytest.approx(0.05)


def test_raw_fallback_reaches_final_review_with_visible_warning(
    tmp_path: Path,
) -> None:
    _, _, result = _run_fixture(
        tmp_path,
        fail_sections={"S02"},
        editorial_provider=_make_editorial_provider(),
    )

    assert result.final_review_path is not None
    assert result.final_review_path.is_file()
    assert result.status == "partial"
    assert result.completed_stage == "publication_mainline_staged_completion"
    assert result.enhanced_sections == ["S01", "S03"]
    assert [entry["section_id"] for entry in result.failed_sections] == ["S02"]
    assert result.handoff_path is not None
    handoff = json.loads(result.handoff_path.read_text(encoding="utf-8"))
    assert set(handoff["sections"]) == {"S01", "S02", "S03"}
    assert handoff["sections"]["S02"]["content_status"] == "raw_fallback"
    assert (
        handoff["sections"]["S02"]["provenance"]["fallback_warning"]
        == "enhancement failed; original section draft retained"
    )
    assert result.stage_metrics["publication_mainline_handoff"][
        "raw_fallback_section_ids"
    ] == ["S02"]
    assert result.commander_work_order_path is not None
    assert result.commander_work_order_path.is_file()
    assert "Legacy pre-enhancement draft for S02." in result.final_review_path.read_text(
        encoding="utf-8"
    )
    assert any("not successfully enhanced" in issue for issue in result.fail_open_issues)
    assert result.summary["delivery_gate"] == "open"


def test_missing_commander_work_order_uses_handoff_order_fail_open(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import optomind_research.runtime.global_manuscript_commander as commander_module

    def missing_work_order(**_: Any) -> dict[str, Any]:
        return {
            "status": "failed",
            "error": "injected commander persistence failure",
        }

    monkeypatch.setattr(
        commander_module,
        "run_global_manuscript_commander",
        missing_work_order,
    )
    _, _, result = _run_fixture(
        tmp_path,
        editorial_provider=_make_editorial_provider(),
    )

    assert result.status == "partial"
    assert result.completed_stage == "publication_mainline_staged_completion"
    assert result.final_review_path is not None
    assert result.final_review_path.is_file()
    final_text = result.final_review_path.read_text(encoding="utf-8")
    assert "Enhanced S01 first paragraph" in final_text
    assert "Legacy pre-enhancement" not in final_text
    assert result.commander_work_order_path is not None
    work_order = json.loads(
        result.commander_work_order_path.read_text(encoding="utf-8")
    )
    assert work_order["status"] == "failed"
    assert work_order["fallback_used"] is True
    assert [
        row["section_id"] for row in work_order["proposed_section_order"]
    ] == ["S01", "S02", "S03"]
    commander_metric = result.stage_metrics[
        "publication_mainline_commander"
    ]
    assert commander_metric["status"] == "failed"
    assert commander_metric["fallback_used"] is True
    assert any(
        "commander final synthesis did not complete" in issue
        for issue in result.fail_open_issues
    )


def test_explicitly_missing_section_blocks_commander_and_closes_delivery_gate(
    tmp_path: Path,
) -> None:
    _, _, result = _run_fixture(
        tmp_path,
        fail_sections={"S02"},
        missing_draft_sections={"S02"},
        editorial_provider=_make_editorial_provider(),
    )

    assert result.final_review_path is None
    assert result.status == "failed"
    assert result.completed_stage == "publication_mainline_handoff"
    assert result.commander_work_order_path is None
    assert result.handoff_path is not None
    handoff = json.loads(result.handoff_path.read_text(encoding="utf-8"))
    assert handoff["sections"]["S02"]["content_status"] == "explicitly_missing"
    assert result.stage_metrics["publication_mainline_handoff"][
        "missing_section_ids"
    ] == ["S02"]
    assert result.summary["delivery_gate"] == "closed"


def test_phase3_authoritative_bindings_preferred_over_empty_evidence_packet(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "phase3"
    project_root.mkdir()
    _write_phase3_authoring_section(project_root, "S01")
    section_dir = project_root / "authoring" / "sections" / "S01"
    packet_path = project_root / "input_packet.json"

    build_enhancer_input_packet(
        section_work_dir=section_dir,
        blueprint_section={
            "section_id": "S01",
            "title": "Phase3 Section",
            "argument_role": "body",
        },
        blueprint={"sections": []},
        output_path=packet_path,
        project_root=project_root,
    )

    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet["evidence_packets"]
    binding = packet["evidence_packets"][0]
    assert (binding["claim_id"], binding["paper_id"], binding["chunk_id"]) == (
        "C1",
        "paper-p1",
        "chunk-p1",
    )
    assert binding["exact_spans"] == ["Verified exact quote from Paper One."]
    assert binding["source_kind"] == "s2_structured_body_snippet"
    assert not any(
        row["claim_id"] == "C1" for row in packet["unresolved_bindings"]
    )
    assert packet["phase3_span_provenance"][0]["span_source"] == "verified_quote"
    assert packet["phase3_span_provenance"][0]["source_locator"]["chunk_id"] == (
        "chunk-p1"
    )


def test_phase3_full_bound_chunk_fallback_is_explicitly_marked(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "phase3-fallback"
    project_root.mkdir()
    phase3_path = _write_phase3_authoring_section(project_root, "S01")
    phase3 = json.loads(phase3_path.read_text(encoding="utf-8"))
    phase3["artifact_refs"]["CLAIM_GRAPH.json"]["path"] = "MISSING_CLAIM_GRAPH.json"
    phase3_path.write_text(json.dumps(phase3, ensure_ascii=False, indent=2))
    local_kb = _write_exact_chunk_db(
        project_root,
        chunk_id="chunk-p1",
        paper_id="paper-p1",
        text="Exact full bound chunk text for chunk-p1.",
    )

    packet_path = project_root / "input_packet.json"
    build_enhancer_input_packet(
        section_work_dir=project_root / "authoring" / "sections" / "S01",
        blueprint_section={
            "section_id": "S01",
            "title": "Phase3 Section",
            "argument_role": "body",
        },
        blueprint={"sections": []},
        output_path=packet_path,
        project_root=project_root,
        local_kb_path=local_kb,
    )

    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet["evidence_packets"][0]["exact_spans"] == [
        "Exact full bound chunk text for chunk-p1."
    ]
    assert packet["evidence_packets"][0]["span_source"] == (
        "full_bound_chunk_fallback"
    )
    assert not packet["unresolved_bindings"]


def test_phase3_claim_graph_checksum_mismatch_is_rejected(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "phase3-wrong-hash"
    project_root.mkdir()
    phase3_path = _write_phase3_authoring_section(project_root, "S01")
    phase3 = json.loads(phase3_path.read_text(encoding="utf-8"))
    phase3["artifact_refs"]["CLAIM_GRAPH.json"]["sha256"] = "0" * 64
    phase3_path.write_text(json.dumps(phase3, ensure_ascii=False, indent=2))
    claim_graph_path = (
        project_root / "phase3_argument_orchestration" / "CLAIM_GRAPH.json"
    )
    wrong_graph = {
        "nodes": [
            {
                "claim_id": "C1",
                "evidence_spans": [
                    {
                        "chunk_id": "chunk-p1",
                        "quote": "Wrong-hash quote must not enter core evidence.",
                        "quote_verified": True,
                    }
                ],
            }
        ]
    }
    claim_graph_path.write_text(json.dumps(wrong_graph), encoding="utf-8")

    packet_path = project_root / "input_packet.json"
    build_enhancer_input_packet(
        section_work_dir=project_root / "authoring" / "sections" / "S01",
        blueprint_section={
            "section_id": "S01",
            "title": "Phase3 Section",
            "argument_role": "body",
        },
        blueprint={"sections": []},
        output_path=packet_path,
        project_root=project_root,
    )

    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet["evidence_packets"] == []
    assert "Wrong-hash quote must not enter core evidence." not in json.dumps(
        packet
    )
    assert any(
        "claim_graph_sha256_mismatch_rejected" in item
        for item in packet["phase3_diagnostics"]
    )
    assert any(
        row["claim_id"] == "C1" and "chunk-p1" in row["chunk_ids"]
        for row in packet["unresolved_bindings"]
    )


def test_local_search_callback_reaches_enhancement_runner(tmp_path: Path) -> None:
    project_root = tmp_path / "callback"
    authoring_root = project_root / "authoring"
    authoring_root.mkdir(parents=True)
    section = _write_r4_section(project_root, "S01", "Callback Section")
    blueprint = {
        "schema_version": "research_harness.review_blueprint.v1",
        "sections": [section],
        "full_review_argument": "Callback review.",
        "input_context": {"user_question": "Callback question."},
    }
    local_calls: list[tuple[str, int]] = []
    observed_callback: dict[str, Any] = {}

    def local_search(query: str, max_results: int) -> list[dict[str, Any]]:
        local_calls.append((query, max_results))
        return []

    def runner(
        packet_path: Path,
        old_draft_path: Path,
        output_dir: Path,
        local_search_callback=None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        observed_callback["callback"] = local_search_callback
        if local_search_callback is not None:
            local_search_callback("callback-query", 3)
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        _write_required_enhanced_artifacts(
            output_dir,
            section_id=str(packet["section_id"]),
            title=str(packet["section_contract"]["title"]),
        )
        return {
            "mode": "live",
            "status": "enhanced",
            "section_id": str(packet["section_id"]),
            "model_usage": {
                "call_count": 1,
                "total_estimated_cost_cny": 0.01,
                "input_tokens": 1,
                "output_tokens": 1,
            },
        }

    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=project_root / "publication_mainline",
        admitted_section_ids=["S01"],
        blueprint=blueprint,
        run_id="callback-run",
        enhancement_live=True,
        enhancement_runner=runner,
        local_search_callback=local_search,
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )

    assert result.status == "completed"
    assert observed_callback["callback"] is local_search
    assert local_calls == [("callback-query", 3)]
    assert (
        result.stage_metrics["publication_mainline_enhancement"][
            "local_search_callback"
        ]
        == "injected"
    )


def test_none_enhancement_qwen_caller_is_not_forwarded(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "caller-none"
    authoring_root = project_root / "authoring"
    authoring_root.mkdir(parents=True)
    section = _write_r4_section(project_root, "S01", "Caller Section")
    blueprint = {
        "schema_version": "research_harness.review_blueprint.v1",
        "sections": [section],
        "full_review_argument": "Caller review.",
        "input_context": {"user_question": "Caller question."},
    }
    missing = object()
    observed: dict[str, Any] = {}

    def runner(
        packet_path: Path,
        old_draft_path: Path,
        output_dir: Path,
        qwen_caller=missing,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        observed["qwen_caller"] = qwen_caller
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        _write_required_enhanced_artifacts(
            output_dir,
            section_id=str(packet["section_id"]),
            title=str(packet["section_contract"]["title"]),
        )
        return {
            "mode": "live",
            "status": "enhanced",
            "section_id": str(packet["section_id"]),
            "model_usage": {
                "call_count": 1,
                "total_estimated_cost_cny": 0.01,
                "input_tokens": 1,
                "output_tokens": 1,
            },
        }

    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=project_root / "publication_mainline",
        admitted_section_ids=["S01"],
        blueprint=blueprint,
        run_id="caller-none-run",
        enhancement_live=True,
        enhancement_runner=runner,
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )

    assert result.status == "completed"
    assert observed["qwen_caller"] is missing


def test_publication_mainline_default_application_settings(
    tmp_path: Path,
) -> None:
    """Default application settings forward 5, 4, 6, 6 to the enhancer."""
    from optomind_research.chapter_asset_enhancer import (
        DEFAULT_APPLICATION_LOCAL_MAX_RESULTS,
        DEFAULT_APPLICATION_MAX_TARGETS,
        DEFAULT_APPLICATION_PER_TARGET_CAP,
        DEFAULT_APPLICATION_SOFT_MIN_TARGETS,
    )

    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "app-defaults"
    )
    observed: dict[str, Any] = {}

    def runner(
        packet_path: Path,
        old_draft_path: Path,
        output_dir: Path,
        live: bool = False,
        local_search_callback=None,
        s2_search_callback=None,
        representative_applications_enabled: bool = False,
        application_max_targets: int = 0,
        application_soft_min_targets: int = 0,
        application_per_target_cap: int = 0,
        application_local_max_results: int = 0,
        application_writer_tier: str = "",
        **_kwargs: Any,
    ) -> dict[str, Any]:
        observed["application_max_targets"] = application_max_targets
        observed["application_soft_min_targets"] = application_soft_min_targets
        observed["application_per_target_cap"] = application_per_target_cap
        observed["application_local_max_results"] = application_local_max_results
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        _write_required_enhanced_artifacts(
            output_dir,
            section_id=str(packet["section_id"]),
            title=str(packet["section_contract"]["title"]),
        )
        return {
            "mode": "live",
            "status": "enhanced",
            "section_id": str(packet["section_id"]),
            "model_usage": {
                "call_count": 1,
                "total_estimated_cost_cny": 0.01,
                "input_tokens": 1,
                "output_tokens": 1,
            },
        }

    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=project_root / "publication_mainline",
        admitted_section_ids=["S01"],
        blueprint=blueprint,
        run_id="app-defaults-run",
        enhancement_live=True,
        enhancement_runner=runner,
        local_search_callback=lambda q, m: [],
        s2_search_callback=lambda q, m: [],
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )

    assert result.status == "completed"
    assert (
        observed["application_max_targets"]
        == DEFAULT_APPLICATION_MAX_TARGETS
    )
    assert (
        observed["application_soft_min_targets"]
        == DEFAULT_APPLICATION_SOFT_MIN_TARGETS
    )
    assert (
        observed["application_per_target_cap"]
        == DEFAULT_APPLICATION_PER_TARGET_CAP
    )
    assert (
        observed["application_local_max_results"]
        == DEFAULT_APPLICATION_LOCAL_MAX_RESULTS
    )

    stage_metrics = result.stage_metrics[
        "publication_mainline_enhancement"
    ]
    settings = stage_metrics["application_settings"]
    assert (
        settings["application_max_targets"]
        == DEFAULT_APPLICATION_MAX_TARGETS
    )
    assert (
        settings["application_soft_min_targets"]
        == DEFAULT_APPLICATION_SOFT_MIN_TARGETS
    )
    assert (
        settings["application_per_target_cap"]
        == DEFAULT_APPLICATION_PER_TARGET_CAP
    )
    assert (
        settings["application_local_max_results"]
        == DEFAULT_APPLICATION_LOCAL_MAX_RESULTS
    )
    assert stage_metrics["application_settings_notes"] == []

def test_publication_mainline_forwards_application_settings_and_callbacks(
    tmp_path: Path,
) -> None:
    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "app-settings"
    )
    observed: dict[str, Any] = {}

    def local_search(query: str, max_results: int) -> list[dict[str, Any]]:
        return []

    def s2_search(query: str, max_results: int) -> list[dict[str, Any]]:
        return []

    def runner(
        packet_path: Path,
        old_draft_path: Path,
        output_dir: Path,
        live: bool = False,
        local_search_callback=None,
        s2_search_callback=None,
        representative_applications_enabled: bool = False,
        application_max_targets: int = 0,
        application_soft_min_targets: int = 0,
        application_per_target_cap: int = 0,
        application_local_max_results: int = 0,
        application_writer_tier: str = "",
        **_kwargs: Any,
    ) -> dict[str, Any]:
        observed["packet_path"] = packet_path
        observed["old_draft_path"] = old_draft_path
        observed["live"] = live
        observed["local_search_callback"] = local_search_callback
        observed["s2_search_callback"] = s2_search_callback
        observed["representative_applications_enabled"] = (
            representative_applications_enabled
        )
        observed["application_max_targets"] = application_max_targets
        observed["application_soft_min_targets"] = application_soft_min_targets
        observed["application_per_target_cap"] = application_per_target_cap
        observed["application_local_max_results"] = (
            application_local_max_results
        )
        observed["application_writer_tier"] = application_writer_tier
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        _write_required_enhanced_artifacts(
            output_dir,
            section_id=str(packet["section_id"]),
            title=str(packet["section_contract"]["title"]),
        )
        return {
            "mode": "live",
            "status": "enhanced",
            "section_id": str(packet["section_id"]),
            "model_usage": {
                "call_count": 1,
                "total_estimated_cost_cny": 0.01,
                "input_tokens": 1,
                "output_tokens": 1,
            },
        }

    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=project_root / "publication_mainline",
        admitted_section_ids=["S01"],
        blueprint=blueprint,
        run_id="app-settings-run",
        enhancement_live=True,
        enhancement_runner=runner,
        local_search_callback=local_search,
        s2_search_callback=s2_search,
        representative_applications_enabled=True,
        application_max_targets=3,
        application_soft_min_targets=5,
        application_per_target_cap=6,
        application_local_max_results=6,
        application_writer_tier="c2_model",
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )

    assert result.status == "completed"
    assert observed["old_draft_path"] == (
        authoring_root / "sections" / "S01" / "SECTION_DRAFT_EN.md"
    )
    assert observed["packet_path"] == (
        project_root
        / "publication_mainline"
        / "enhancement"
        / "S01"
        / "input_packet.json"
    )
    assert observed["live"] is True
    assert observed["local_search_callback"] is local_search
    assert observed["s2_search_callback"] is s2_search
    assert observed["representative_applications_enabled"] is True
    assert observed["application_max_targets"] == 3
    assert observed["application_soft_min_targets"] == 5
    assert observed["application_per_target_cap"] == 6
    assert observed["application_local_max_results"] == 6
    assert observed["application_writer_tier"] == "c2_model"

    stage_metrics = result.stage_metrics[
        "publication_mainline_enhancement"
    ]
    assert stage_metrics["application_settings"] == {
        "representative_applications_enabled": True,
        "application_max_targets": 3,
        "application_soft_min_targets": 5,
        "application_per_target_cap": 6,
        "application_local_max_results": 6,
        "application_writer_tier": "c2_model",
    }
    assert stage_metrics["application_settings_notes"] == []
    reuse_state = json.loads(
        (
            project_root
            / "publication_mainline"
            / "enhancement"
            / "S01"
            / ENHANCEMENT_REUSE_STATE_JSON
        ).read_text(encoding="utf-8")
    )
    assert reuse_state["application_settings"] == stage_metrics[
        "application_settings"
    ]

    handoff = json.loads(result.handoff_path.read_text(encoding="utf-8"))
    envelope = handoff["sections"]["S01"]
    assert envelope["enhanced_chapter"]["path"].endswith(
        "ENHANCED_CHAPTER.md"
    )
    assert envelope["explanatory_citation_ledger"]["path"].endswith(
        "EXPLANATORY_CITATION_LEDGER.json"
    )
    assert (
        envelope["explanatory_citation_ledger"]["trust_boundary"]
        == "background_explanation_only"
    )


def test_publication_mainline_missing_application_example_does_not_fail(
    tmp_path: Path,
) -> None:
    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "app-missing"
    )

    def runner(
        packet_path: Path,
        old_draft_path: Path,
        output_dir: Path,
        live: bool = False,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        _write_required_enhanced_artifacts(
            output_dir,
            section_id=str(packet["section_id"]),
            title=str(packet["section_contract"]["title"]),
        )
        return {
            "mode": "live",
            "status": "enhanced",
            "section_id": str(packet["section_id"]),
            "model_usage": {
                "call_count": 1,
                "total_estimated_cost_cny": 0.01,
                "input_tokens": 1,
                "output_tokens": 1,
            },
            "representative_application_metrics": {
                "targets_planned": 1,
                "skipped_targets": 1,
                "examples_attached": 0,
                "application_writer_call_count": 0,
            },
        }

    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=project_root / "publication_mainline",
        admitted_section_ids=["S01"],
        blueprint=blueprint,
        run_id="app-missing-run",
        enhancement_live=True,
        enhancement_runner=runner,
        local_search_callback=lambda query, max_results: [],
        s2_search_callback=lambda query, max_results: [],
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )

    assert result.status == "completed"
    assert result.enhanced_sections == ["S01"]
    assert result.failed_sections == []
    assert result.final_review_path is not None
    assert result.final_review_path.is_file()


def test_application_settings_change_invalidates_enhancement_reuse(
    tmp_path: Path,
) -> None:
    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "app-reuse-invalid"
    )
    calls: list[str] = []

    def runner(
        packet_path: Path,
        old_draft_path: Path,
        output_dir: Path,
        live: bool = False,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        calls.append("enhance")
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        _write_required_enhanced_artifacts(
            output_dir,
            section_id=str(packet["section_id"]),
            title=str(packet["section_contract"]["title"]),
        )
        return {
            "mode": "live",
            "status": "enhanced",
            "section_id": str(packet["section_id"]),
            "model_usage": {
                "call_count": 1,
                "total_estimated_cost_cny": 0.01,
                "input_tokens": 1,
                "output_tokens": 1,
            },
        }

    common = {
        "project_root": project_root,
        "authoring_work_dir": authoring_root,
        "output_root": project_root / "publication_mainline",
        "admitted_section_ids": ["S01"],
        "blueprint": blueprint,
        "run_id": "app-reuse-invalid-run",
        "enhancement_live": True,
        "enhancement_runner": runner,
        "commander_live": False,
        "commander_role_provider": _CommanderProvider(),
        "staged_live": False,
    }

    first = run_publication_mainline(**common)
    assert first.status == "completed"
    assert calls == ["enhance"]

    second = run_publication_mainline(**common)
    assert second.status == "completed"
    assert calls == ["enhance"]
    second_metrics = second.stage_metrics[
        "publication_mainline_enhancement"
    ]["sections"]["S01"]
    assert second_metrics["reused"] is True

    third = run_publication_mainline(
        **common,
        application_max_targets=1,
    )
    assert third.status == "completed"
    assert calls == ["enhance", "enhance"]
    third_metrics = third.stage_metrics[
        "publication_mainline_enhancement"
    ]["sections"]["S01"]
    assert third_metrics.get("reused") is not True

    fourth = run_publication_mainline(
        **common,
        application_soft_min_targets=5,
    )
    assert fourth.status == "completed"
    assert calls == ["enhance", "enhance", "enhance"]
    fourth_metrics = fourth.stage_metrics[
        "publication_mainline_enhancement"
    ]["sections"]["S01"]
    assert fourth_metrics.get("reused") is not True
    assert fourth.stage_metrics["publication_mainline_enhancement"][
        "application_settings"
    ]["application_soft_min_targets"] == 5


def test_cli_exposes_publication_mainline_soft_min_flag() -> None:
    import subprocess
    import sys

    project_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, "run_review_harness.py", "--help"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0
    assert (
        "--publication-mainline-application-soft-min-targets"
        in completed.stdout
    )


def test_enhancement_fanout_parallel_deterministic_retry_reuse(
    tmp_path: Path,
) -> None:
    import threading

    project_root = tmp_path / "fanout"
    authoring_root = project_root / "authoring"
    authoring_root.mkdir(parents=True)
    sections = [
        _write_r4_section(project_root, section_id, f"Section {section_id}")
        for section_id in ("S01", "S02", "S03")
    ]
    blueprint = {
        "schema_version": "research_harness.review_blueprint.v1",
        "sections": sections,
        "full_review_argument": "fanout review",
        "input_context": {"user_question": "fanout question"},
    }
    lock = threading.Lock()
    calls: dict[str, int] = {}
    active = {"value": 0}
    max_active = {"value": 0}
    fail_once = {"S02": True}

    def runner(
        packet_path: Path,
        old_draft_path: Path,
        output_dir: Path,
        live: bool = False,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        section_id = str(
            json.loads(packet_path.read_text(encoding="utf-8"))[
                "section_id"
            ]
        )
        with lock:
            calls[section_id] = calls.get(section_id, 0) + 1
            active["value"] += 1
            max_active["value"] = max(
                max_active["value"], active["value"]
            )
        try:
            if fail_once.get(section_id):
                fail_once[section_id] = False
                raise TimeoutError("transient transport failure")
            _write_required_enhanced_artifacts(
                output_dir,
                section_id=section_id,
                title=f"Section {section_id}",
            )
            return {
                "mode": "live",
                "status": "enhanced",
                "section_id": section_id,
                "model_usage": {
                    "call_count": 1,
                    "total_estimated_cost_cny": 0.01,
                    "input_tokens": 1,
                    "output_tokens": 1,
                },
            }
        finally:
            with lock:
                active["value"] -= 1

    common = {
        "project_root": project_root,
        "authoring_work_dir": authoring_root,
        "output_root": project_root / "publication_mainline",
        "admitted_section_ids": ["S01", "S02", "S03"],
        "blueprint": blueprint,
        "run_id": "fanout-run",
        "enhancement_live": True,
        "enhancement_runner": runner,
        "enhancement_workers": 3,
        "enhancement_transient_retry_rounds": 1,
        "commander_live": False,
        "commander_role_provider": _CommanderProvider(),
        "staged_live": False,
    }
    first = run_publication_mainline(**common)

    assert first.status == "completed"
    # Within a round results merge in section order; the transiently failed
    # section is appended in its retry round (same as the serial behavior).
    assert first.enhanced_sections == ["S01", "S03", "S02"]
    assert max_active["value"] >= 2
    assert calls == {"S01": 1, "S02": 2, "S03": 1}
    assert (
        first.stage_metrics["publication_mainline_enhancement"][
            "enhancement_workers"
        ]
        == 3
    )

    second = run_publication_mainline(**common)
    assert second.status == "completed"
    # Reuse fingerprints: no section calls the runner again.
    assert calls == {"S01": 1, "S02": 2, "S03": 1}
    second_metrics = second.stage_metrics[
        "publication_mainline_enhancement"
    ]["sections"]
    assert all(row["reused"] is True for row in second_metrics.values())


def test_injected_enhancement_qwen_caller_is_forwarded(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "caller-injected"
    authoring_root = project_root / "authoring"
    authoring_root.mkdir(parents=True)
    section = _write_r4_section(project_root, "S01", "Injected Caller Section")
    blueprint = {
        "schema_version": "research_harness.review_blueprint.v1",
        "sections": [section],
        "full_review_argument": "Injected caller review.",
        "input_context": {"user_question": "Injected caller question."},
    }
    fake_caller = lambda **_: {"fake": "response"}
    observed: dict[str, Any] = {}

    def runner(
        packet_path: Path,
        old_draft_path: Path,
        output_dir: Path,
        qwen_caller=None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        observed["qwen_caller"] = qwen_caller
        assert callable(qwen_caller)
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        _write_required_enhanced_artifacts(
            output_dir,
            section_id=str(packet["section_id"]),
            title=str(packet["section_contract"]["title"]),
        )
        return {
            "mode": "live",
            "status": "enhanced",
            "section_id": str(packet["section_id"]),
            "model_usage": {
                "call_count": 1,
                "total_estimated_cost_cny": 0.01,
                "input_tokens": 1,
                "output_tokens": 1,
            },
        }

    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=project_root / "publication_mainline",
        admitted_section_ids=["S01"],
        blueprint=blueprint,
        run_id="caller-injected-run",
        enhancement_live=True,
        enhancement_runner=runner,
        enhancement_qwen_caller=fake_caller,
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )

    assert result.status == "completed"
    assert observed["qwen_caller"] is fake_caller


def test_enhancement_reuse_when_fingerprint_matches(
    tmp_path: Path,
) -> None:
    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "reuse-match"
    )
    base_runner = _make_fake_enhancement_runner()
    calls: list[str] = []

    def runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append("enhance")
        return base_runner(*args, **kwargs)

    common = {
        "project_root": project_root,
        "authoring_work_dir": authoring_root,
        "output_root": project_root / "publication_mainline",
        "admitted_section_ids": ["S01"],
        "blueprint": blueprint,
        "run_id": "reuse-match-run",
        "enhancement_live": True,
        "enhancement_runner": runner,
        "commander_live": False,
        "commander_role_provider": _CommanderProvider(),
        "staged_live": False,
    }

    first = run_publication_mainline(**common)
    assert first.status == "completed"
    assert calls == ["enhance"]

    second = run_publication_mainline(**common)
    assert second.status == "completed"
    assert calls == ["enhance"]
    section_metrics = second.stage_metrics[
        "publication_mainline_enhancement"
    ]["sections"]["S01"]
    assert section_metrics["reused"] is True
    assert section_metrics["fingerprint_matched"] is True
    assert section_metrics["cost_cny"] == 0.0
    assert section_metrics["input_tokens"] == 0
    assert section_metrics["output_tokens"] == 0


def test_enhancement_reruns_when_fingerprint_mismatches(
    tmp_path: Path,
) -> None:
    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "reuse-mismatch"
    )
    base_runner = _make_fake_enhancement_runner()
    calls: list[str] = []

    def runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append("enhance")
        return base_runner(*args, **kwargs)

    common = {
        "project_root": project_root,
        "authoring_work_dir": authoring_root,
        "output_root": project_root / "publication_mainline",
        "admitted_section_ids": ["S01"],
        "blueprint": blueprint,
        "run_id": "reuse-mismatch-run",
        "enhancement_live": True,
        "enhancement_runner": runner,
        "commander_live": False,
        "commander_role_provider": _CommanderProvider(),
        "staged_live": False,
    }

    first = run_publication_mainline(**common)
    assert first.status == "completed"
    assert calls == ["enhance"]

    old_draft = (
        authoring_root / "sections" / "S01" / "SECTION_DRAFT_EN.md"
    )
    old_draft.write_text(
        "A changed authoritative pre-enhancement draft.",
        encoding="utf-8",
    )

    second = run_publication_mainline(**common)
    assert second.status == "completed"
    assert calls == ["enhance", "enhance"]
    section_metrics = second.stage_metrics[
        "publication_mainline_enhancement"
    ]["sections"]["S01"]
    assert section_metrics.get("reused") is not True


def test_enhancement_reruns_when_prompt_fingerprint_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "reuse-prompt-mismatch"
    )
    base_runner = _make_fake_enhancement_runner()
    calls: list[str] = []

    def runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append("enhance")
        return base_runner(*args, **kwargs)

    common = {
        "project_root": project_root,
        "authoring_work_dir": authoring_root,
        "output_root": project_root / "publication_mainline",
        "admitted_section_ids": ["S01"],
        "blueprint": blueprint,
        "run_id": "reuse-prompt-mismatch-run",
        "enhancement_live": True,
        "enhancement_runner": runner,
        "commander_live": False,
        "commander_role_provider": _CommanderProvider(),
        "staged_live": False,
    }

    assert run_publication_mainline(**common).status == "completed"
    assert calls == ["enhance"]

    monkeypatch.setattr(
        chapter_enhancer,
        "enhancement_contract_fingerprint",
        lambda: {"changed_prompt.txt": "new-hash"},
    )

    second = run_publication_mainline(**common)
    assert second.status == "completed"
    assert calls == ["enhance", "enhance"]


def test_transient_enhancement_failure_is_retried_then_recovers(
    tmp_path: Path,
) -> None:
    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "transient-retry"
    )
    base_runner = _make_fake_enhancement_runner()
    calls: list[str] = []

    def runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append("enhance")
        if len(calls) == 1:
            raise TimeoutError("temporary transport timeout")
        return base_runner(*args, **kwargs)

    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=project_root / "publication_mainline",
        admitted_section_ids=["S01"],
        blueprint=blueprint,
        run_id="transient-retry-run",
        enhancement_live=True,
        enhancement_runner=runner,
        enhancement_transient_retry_rounds=2,
        enhancement_transient_retry_delay_seconds=0.0,
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )

    assert result.status == "completed"
    assert calls == ["enhance", "enhance"]
    assert result.stage_metrics["publication_mainline_enhancement"][
        "sections"
    ]["S01"]["retry_rounds"] == 1


def test_transient_report_fallback_is_retried_then_recovers(
    tmp_path: Path,
) -> None:
    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "transient-report-retry"
    )
    base_runner = _make_fake_enhancement_runner()
    calls: list[str] = []

    def runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append("enhance")
        if len(calls) == 1:
            return {
                "mode": "live",
                "status": "fail_open_original",
                "section_id": "S01",
                "model_usage": {
                    "calls": [{"error": "URLError"}],
                },
            }
        return base_runner(*args, **kwargs)

    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=project_root / "publication_mainline",
        admitted_section_ids=["S01"],
        blueprint=blueprint,
        run_id="transient-report-retry-run",
        enhancement_live=True,
        enhancement_runner=runner,
        enhancement_transient_retry_rounds=2,
        enhancement_transient_retry_delay_seconds=0.0,
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )

    assert result.status == "completed"
    assert calls == ["enhance", "enhance"]
    assert result.stage_metrics["publication_mainline_enhancement"][
        "sections"
    ]["S01"]["retry_rounds"] == 1


def test_persistent_transient_enhancement_failure_stops_at_bound(
    tmp_path: Path,
) -> None:
    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "transient-persistent"
    )
    calls: list[str] = []

    def runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append("enhance")
        raise TimeoutError("persistent transport timeout")

    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=project_root / "publication_mainline",
        admitted_section_ids=["S01"],
        blueprint=blueprint,
        run_id="transient-persistent-run",
        enhancement_live=True,
        enhancement_runner=runner,
        enhancement_transient_retry_rounds=2,
        enhancement_transient_retry_delay_seconds=0.0,
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )

    assert result.status == "failed"
    assert calls == ["enhance", "enhance", "enhance"]
    assert result.failed_sections[0]["retry_rounds"] == 2
    assert result.failed_sections[0]["transport_errors"] == []


def test_nontransient_enhancement_failure_is_not_retried(
    tmp_path: Path,
) -> None:
    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "nontransient"
    )
    calls: list[str] = []

    def runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append("enhance")
        raise ValueError("non-transient scientific/integrity failure")

    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=project_root / "publication_mainline",
        admitted_section_ids=["S01"],
        blueprint=blueprint,
        run_id="nontransient-run",
        enhancement_live=True,
        enhancement_runner=runner,
        enhancement_transient_retry_rounds=2,
        enhancement_transient_retry_delay_seconds=0.0,
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )

    assert result.status == "failed"
    assert calls == ["enhance"]
    assert result.failed_sections[0]["retry_rounds"] == 0


def test_adapter_local_metadata_callback_is_read_only_for_papers(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "adapter-papers.sqlite"
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
            INSERT INTO papers VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "paper-adapter",
                "10.1000/adapter",
                "Adapter Readonly Paper",
                2024,
                "Test Venue",
                "adapter readonly metadata",
                json.dumps({"authors": ["Alice", "Bob"], "abstract": "Adapter."}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    before = db_path.read_bytes()
    callback = _LocalMetadataCallback(db_path)
    rows = callback("adapter readonly", 5)
    callback("adapter readonly", 5)
    callback.close()
    after = db_path.read_bytes()

    assert len(rows) == 1
    assert rows[0]["paper_id"] == "paper-adapter"
    assert after == before


def test_adapter_local_metadata_callback_is_read_only_for_abstract_papers(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "adapter-abstract.sqlite"
    conn = sqlite3.connect(str(db_path))
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
                paper_id, doi, title, authors_json, year, venue, abstract,
                raw_json, source_apis_json, query_used_json,
                matched_keywords_json, topic_tags_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "paper-adapter-abstract",
                "10.1000/adapter-abstract",
                "Adapter Abstract Readonly Paper",
                json.dumps(["Alice Example"]),
                2025,
                "Test Venue",
                "Adapter abstract readonly.",
                json.dumps({"source_audit": {"status": "accepted"}}),
                json.dumps(["s2"]),
                json.dumps(["adapter"]),
                json.dumps(["metadata"]),
                json.dumps(["review"]),
                "2025-01-01T00:00:00",
                "2025-01-01T00:00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    before = db_path.read_bytes()
    callback = _LocalMetadataCallback(db_path)
    rows = callback("adapter abstract readonly", 5)
    callback("adapter abstract readonly", 5)
    callback.close()
    after = db_path.read_bytes()

    assert len(rows) == 1
    assert rows[0]["paper_id"] == "paper-adapter-abstract"
    assert rows[0]["authors"] == ["Alice Example"]
    assert after == before


def test_adapter_empty_abstract_schema_falls_back_to_papers_read_only(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "adapter-empty-abstract.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE abstract_papers (
                paper_id TEXT, title TEXT, year INTEGER, venue TEXT,
                abstract TEXT, authors_json TEXT, raw_json TEXT
            )
            """
        )
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
                "paper-fallback",
                "10.1000/fallback",
                "Fallback Readonly Paper",
                2024,
                "Test Venue",
                "fallback readonly metadata",
                json.dumps({"authors": ["Alice"], "abstract": "Fallback."}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    before = db_path.read_bytes()
    callback = _LocalMetadataCallback(db_path)
    rows = callback("fallback readonly", 5)
    callback.close()
    after = db_path.read_bytes()

    assert len(rows) == 1
    assert rows[0]["paper_id"] == "paper-fallback"
    assert after == before


def test_local_metadata_callback_normalizes_mixed_author_entries(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "metadata.sqlite"
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
                "paper-mixed-authors",
                "10.1000/mixed-authors",
                "Mixed Author Paper",
                2025,
                "Test Venue",
                "mixed author metadata",
                json.dumps(
                    {
                        "authors": [
                            {"name": "Alice Example"},
                            "Bob Example",
                            {"display_name": "Carol Example"},
                            {"author": "Dan Example"},
                            "",
                            None,
                        ],
                        "abstract": "A useful abstract for mixed authors.",
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    callback = _LocalMetadataCallback(db_path)
    try:
        records = callback("mixed author metadata", 5)
    finally:
        callback.close()

    assert len(records) == 1
    assert records[0]["paper_id"] == "paper-mixed-authors"
    assert records[0]["authors"] == [
        "Alice Example",
        "Bob Example",
        "Carol Example",
        "Dan Example",
    ]
    assert records[0]["abstract"] == "A useful abstract for mixed authors."


class _FakeS2MetadataBackend:
    """Offline stand-in for SemanticScholarBackend (no network)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.closed = False

    def search(self, query: str, max_results: int) -> list[dict[str, Any]]:
        self.calls.append((query, max_results))
        return [
            {
                "semantic_scholar_paper_id": "s2-fallback-1",
                "title": "Fallback Application Paper",
                "authors": ["F. Author"],
                "year": 2023,
                "abstract_or_snippet": (
                    "A PINN application recovered boundary values from "
                    "sparse sensor data."
                ),
                "journal_or_venue": "Applied Optics",
                "url_or_doi": "https://doi.org/10.1000/fallback",
                "doi": "10.1000/fallback",
            }
        ]

    def close(self) -> None:
        self.closed = True


def test_owned_s2_metadata_callback_serializes_shared_backend(
    monkeypatch,
) -> None:
    import optomind_research.runtime.publication_mainline_adapter as adapter

    instances: list[Any] = []

    class ReentrantDetectingBackend:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []
            self.active = 0
            self.reentered = False
            self.lock = threading.Lock()

        def search(self, query: str, max_results: int) -> list[dict[str, Any]]:
            with self.lock:
                if self.active:
                    self.reentered = True
                self.active += 1
            try:
                time.sleep(0.01)
                self.calls.append((query, max_results))
                return [{"title": query}]
            finally:
                with self.lock:
                    self.active -= 1

    def fake_backend_class():
        backend = ReentrantDetectingBackend()
        instances.append(backend)
        return backend

    monkeypatch.setattr(
        "tools.academic_backends.semantic_scholar_backend"
        ".SemanticScholarBackend",
        fake_backend_class,
    )
    callback = adapter._build_owned_s2_metadata_callback(per_target_cap=6)
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(
            executor.map(
                lambda index: callback(f"query {index}", 6),
                range(3),
            )
        )

    assert len(instances) == 1
    assert [row[0]["title"] for row in results] == [
        "query 0",
        "query 1",
        "query 2",
    ]
    assert instances[0].reentered is False
    assert instances[0].calls == [
        ("query 0", 6),
        ("query 1", 6),
        ("query 2", 6),
    ]


def _s2_fallback_runner(local_hit: bool, observed: dict[str, Any]):
    def runner(
        packet_path: Path,
        old_draft_path: Path,
        output_dir: Path,
        live: bool = False,
        local_search_callback=None,
        s2_search_callback=None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        observed["local_search_callback"] = local_search_callback
        observed["s2_search_callback"] = s2_search_callback
        if local_hit:
            # A usable local candidate means the enhancer never asks S2; the
            # owned callback may exist but must never be invoked.
            pass
        else:
            assert s2_search_callback is not None
            # Simulate a local miss; request more than the cap to prove the
            # callback bounds the request defensively.
            s2_search_callback("fallback query", 9)
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        _write_required_enhanced_artifacts(
            output_dir,
            section_id=str(packet["section_id"]),
            title=str(packet["section_contract"]["title"]),
        )
        return {
            "mode": "live",
            "status": "enhanced",
            "section_id": str(packet["section_id"]),
            "model_usage": {
                "call_count": 1,
                "total_estimated_cost_cny": 0.01,
                "input_tokens": 1,
                "output_tokens": 1,
            },
        }

    return runner


def test_default_s2_metadata_fallback_used_only_on_local_miss(
    tmp_path: Path,
    monkeypatch,
) -> None:
    instances: list[_FakeS2MetadataBackend] = []

    def fake_backend_class():
        backend = _FakeS2MetadataBackend()
        instances.append(backend)
        return backend

    monkeypatch.setattr(
        "tools.academic_backends.semantic_scholar_backend"
        ".SemanticScholarBackend",
        fake_backend_class,
    )

    # Local miss: the owned fallback is invoked exactly once, bounded to <=6.
    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "s2-miss"
    )
    observed: dict[str, Any] = {}
    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=project_root / "publication_mainline",
        admitted_section_ids=["S01"],
        blueprint=blueprint,
        run_id="s2-miss-run",
        enhancement_live=True,
        enhancement_runner=_s2_fallback_runner(
            local_hit=False, observed=observed
        ),
        local_search_callback=lambda query, max_results: [],
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )
    assert result.status == "completed"
    assert len(instances) == 1
    assert instances[0].calls == [("fallback query", 6)]
    assert observed["s2_search_callback"] is not None
    stage_metrics = result.stage_metrics[
        "publication_mainline_enhancement"
    ]
    assert stage_metrics["s2_search_callback"] == "configured_default"
    assert stage_metrics["s2_metadata_fallback_cap"] == 6
    assert stage_metrics["s2_metadata_fallback_notes"] == []
    assert instances[0].closed is True

    # Local hit: no S2 request at all.
    project_root2, authoring_root2, blueprint2 = _single_section_fixture(
        tmp_path, "s2-hit"
    )
    observed2: dict[str, Any] = {}
    result2 = run_publication_mainline(
        project_root=project_root2,
        authoring_work_dir=authoring_root2,
        output_root=project_root2 / "publication_mainline",
        admitted_section_ids=["S01"],
        blueprint=blueprint2,
        run_id="s2-hit-run",
        enhancement_live=True,
        enhancement_runner=_s2_fallback_runner(
            local_hit=True, observed=observed2
        ),
        local_search_callback=lambda query, max_results: [
            {
                "paper_id": "local-paper",
                "title": "Local Application Paper",
                "abstract": (
                    "PINN application recovering boundary values from "
                    "sparse sensor data."
                ),
                "relevance_score": 0.9,
            }
        ],
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )
    assert result2.status == "completed"
    assert len(instances) == 2
    assert instances[1].calls == []


def test_default_s2_metadata_fallback_disabled_prevents_construction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def explode():
        raise AssertionError("default S2 backend must not be constructed")

    monkeypatch.setattr(
        "tools.academic_backends.semantic_scholar_backend"
        ".SemanticScholarBackend",
        explode,
    )
    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "s2-disabled"
    )
    observed: dict[str, Any] = {}
    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=project_root / "publication_mainline",
        admitted_section_ids=["S01"],
        blueprint=blueprint,
        run_id="s2-disabled-run",
        enhancement_live=True,
        enhancement_runner=_s2_fallback_runner(
            local_hit=True, observed=observed
        ),
        s2_metadata_fallback_enabled=False,
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )
    assert result.status == "completed"
    assert observed["s2_search_callback"] is None
    stage_metrics = result.stage_metrics[
        "publication_mainline_enhancement"
    ]
    assert stage_metrics["s2_search_callback"] == "disabled"


def test_injected_s2_callback_takes_precedence_over_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def explode():
        raise AssertionError("default S2 backend must not be constructed")

    monkeypatch.setattr(
        "tools.academic_backends.semantic_scholar_backend"
        ".SemanticScholarBackend",
        explode,
    )
    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "s2-injected"
    )
    injected_calls: list[tuple[str, int]] = []

    def injected(query: str, max_results: int) -> list[dict[str, Any]]:
        injected_calls.append((query, max_results))
        return []

    observed: dict[str, Any] = {}
    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=project_root / "publication_mainline",
        admitted_section_ids=["S01"],
        blueprint=blueprint,
        run_id="s2-injected-run",
        enhancement_live=True,
        enhancement_runner=_s2_fallback_runner(
            local_hit=False, observed=observed
        ),
        s2_search_callback=injected,
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )
    assert result.status == "completed"
    assert observed["s2_search_callback"] is injected
    assert injected_calls == [("fallback query", 9)]
    stage_metrics = result.stage_metrics[
        "publication_mainline_enhancement"
    ]
    assert stage_metrics["s2_search_callback"] == "injected"


def test_application_per_target_cap_is_clamped_to_six_with_note(
    tmp_path: Path,
) -> None:
    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "s2-cap-clamp"
    )
    observed: dict[str, Any] = {}
    s2_requests: list[tuple[str, int]] = []

    def injected_s2(query: str, max_results: int) -> list[dict[str, Any]]:
        s2_requests.append((query, max_results))
        return []

    def runner(
        packet_path: Path,
        old_draft_path: Path,
        output_dir: Path,
        live: bool = False,
        s2_search_callback=None,
        application_per_target_cap: int = 0,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        observed["application_per_target_cap"] = application_per_target_cap
        # Simulate the enhancer asking S2 with its effective per-target cap.
        if s2_search_callback is not None:
            s2_search_callback("clamp query", application_per_target_cap)
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        _write_required_enhanced_artifacts(
            output_dir,
            section_id=str(packet["section_id"]),
            title=str(packet["section_contract"]["title"]),
        )
        return {
            "mode": "live",
            "status": "enhanced",
            "section_id": str(packet["section_id"]),
            "model_usage": {
                "call_count": 1,
                "total_estimated_cost_cny": 0.01,
                "input_tokens": 1,
                "output_tokens": 1,
            },
        }

    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=project_root / "publication_mainline",
        admitted_section_ids=["S01"],
        blueprint=blueprint,
        run_id="s2-cap-clamp-run",
        enhancement_live=True,
        enhancement_runner=runner,
        s2_search_callback=injected_s2,
        application_per_target_cap=7,
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )

    assert result.status == "completed"
    assert observed["application_per_target_cap"] == 6
    assert s2_requests == [("clamp query", 6)]
    stage_metrics = result.stage_metrics[
        "publication_mainline_enhancement"
    ]
    assert stage_metrics["application_settings"][
        "application_per_target_cap"
    ] == 6
    assert stage_metrics["s2_metadata_fallback_cap"] == 6
    assert any(
        "application_per_target_cap_clamped_from_7_to_6" in note
        for note in stage_metrics["application_settings_notes"]
    )


def test_application_local_max_results_is_clamped_to_six_with_note(
    tmp_path: Path,
) -> None:
    """Passing local_max=7 is clamped to 6 with a note."""
    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "local-max-clamp"
    )
    observed: dict[str, Any] = {}

    def runner(
        packet_path: Path,
        old_draft_path: Path,
        output_dir: Path,
        live: bool = False,
        application_local_max_results: int = 0,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        observed["application_local_max_results"] = application_local_max_results
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        _write_required_enhanced_artifacts(
            output_dir,
            section_id=str(packet["section_id"]),
            title=str(packet["section_contract"]["title"]),
        )
        return {
            "mode": "live",
            "status": "enhanced",
            "section_id": str(packet["section_id"]),
            "model_usage": {
                "call_count": 1,
                "total_estimated_cost_cny": 0.01,
                "input_tokens": 1,
                "output_tokens": 1,
            },
        }

    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=project_root / "publication_mainline",
        admitted_section_ids=["S01"],
        blueprint=blueprint,
        run_id="local-max-clamp-run",
        enhancement_live=True,
        enhancement_runner=runner,
        s2_search_callback=lambda q, m: [],
        application_local_max_results=7,
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )

    assert result.status == "completed"
    assert observed["application_local_max_results"] == 6
    stage_metrics = result.stage_metrics[
        "publication_mainline_enhancement"
    ]
    assert stage_metrics["application_settings"][
        "application_local_max_results"
    ] == 6
    assert any(
        "application_local_max_results_clamped_from_7_to_6" in note
        for note in stage_metrics["application_settings_notes"]
    )

def test_default_s2_metadata_fallback_construction_failure_fails_open(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def explode():
        raise RuntimeError("backend construction failed")

    monkeypatch.setattr(
        "tools.academic_backends.semantic_scholar_backend"
        ".SemanticScholarBackend",
        explode,
    )
    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "s2-unavailable"
    )
    observed: dict[str, Any] = {}
    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=project_root / "publication_mainline",
        admitted_section_ids=["S01"],
        blueprint=blueprint,
        run_id="s2-unavailable-run",
        enhancement_live=True,
        enhancement_runner=_s2_fallback_runner(
            local_hit=True, observed=observed
        ),
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )
    assert result.status == "completed"
    assert observed["s2_search_callback"] is None
    stage_metrics = result.stage_metrics[
        "publication_mainline_enhancement"
    ]
    assert stage_metrics["s2_search_callback"] == "unavailable"
    assert any(
        "s2_metadata_fallback_unavailable" in note
        for note in stage_metrics["s2_metadata_fallback_notes"]
    )


# ---------------------------------------------------------------------------
# Repair 2 — delivery_gate in PublicationMainlineResult.summary
# ---------------------------------------------------------------------------

def test_summary_contains_delivery_gate_closed_when_no_final_review(
    tmp_path: Path,
) -> None:
    """summary['delivery_gate'] is 'closed' when the final review file is absent."""
    from optomind_research.runtime.publication_mainline_adapter import (
        run_publication_mainline,
    )
    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "dg-closed"
    )
    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=project_root / "publication_mainline",
        admitted_section_ids=["S01"],
        blueprint=blueprint,
        run_id="dg-closed-run",
        enhancement_live=False,
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )
    assert "delivery_gate" in result.summary
    assert result.summary["delivery_gate_path"] is not None


def test_summary_delivery_gate_open_when_final_review_exists(
    tmp_path: Path,
) -> None:
    """summary['delivery_gate'] is 'open' when the final review file is non-empty."""
    from optomind_research.runtime.publication_mainline_adapter import (
        run_publication_mainline,
    )
    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "dg-open"
    )
    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=project_root / "publication_mainline",
        admitted_section_ids=["S01"],
        blueprint=blueprint,
        run_id="dg-open-run",
        enhancement_live=False,
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )
    assert "delivery_gate" in result.summary
    if result.summary["delivery_gate"] == "open":
        gate_path = Path(result.summary["delivery_gate_path"])
        assert gate_path.is_file() and gate_path.stat().st_size > 0


# ---------------------------------------------------------------------------
# Repair 3 — visual_remount live provider is registered
# ---------------------------------------------------------------------------

def test_visual_remount_offline_noop_includes_manifest_key() -> None:
    """Offline visual_remount stage provider returns visual_procurement_manifest."""
    from optomind_research.runtime.staged_article_completion import (
        _default_offline_stage_provider,
    )
    result = _default_offline_stage_provider({"stage": "visual_remount", "inputs": {}})
    assert "visual_procurement_manifest" in result
    manifest = result["visual_procurement_manifest"]
    assert manifest.get("schema_version") == "optomind.visual_procurement_manifest.v1"
    assert manifest.get("fail_open") is True


def test_visual_procurement_pre_step_no_papers_is_skip() -> None:
    """visual_procurement_pre_step with no papers returns status='skip' fail-open."""
    from optomind_research.runtime.staged_article_completion import (
        visual_procurement_pre_step,
    )
    result = visual_procurement_pre_step({})
    assert result.get("fail_open") is True
    assert result.get("status") in {"skip", "offline_noop", "import_error"}


# ---------------------------------------------------------------------------
# Repair 4 — article_title in summary derived from title plan
# ---------------------------------------------------------------------------

def test_summary_contains_article_title(tmp_path: Path) -> None:
    """summary['article_title'] is present and not the raw user question verbatim."""
    from optomind_research.runtime.publication_mainline_adapter import (
        run_publication_mainline,
    )
    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "art-title"
    )
    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=project_root / "publication_mainline",
        admitted_section_ids=["S01"],
        blueprint=blueprint,
        run_id="art-title-run",
        enhancement_live=False,
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )
    assert "article_title" in result.summary
    # The title plan always produces a formatted candidate — value is a string
    assert isinstance(result.summary["article_title"], str)


def test_plan_review_titles_selected_title_is_not_empty() -> None:
    """plan_review_titles always returns a non-empty selected_title."""
    from optomind_research.runtime.staged_article_completion import (
        LiteratureReviewPresentationIR,
        plan_review_titles,
    )
    ir = LiteratureReviewPresentationIR(
        central_topic="What are the optical properties of metasurfaces?",
        review_subtype="critical literature review",
    )
    plan = plan_review_titles(ir)
    assert plan.selected_title
    assert len(plan.candidates) >= 3
    # Must not be a verbatim copy of the raw question
    assert plan.selected_title != ir.central_topic


# Fix 4 — visual_remount live provider receives papers from handoff_data
# ---------------------------------------------------------------------------

def test_visual_procurement_pre_step_with_handoff_papers_passes_paper_list(
    tmp_path: Path,
) -> None:
    """Papers extracted from handoff sections reach visual_procurement_pre_step.

    The provider augments stage_inputs with papers from handoff_data when the
    stage carries none.  We verify this by calling visual_procurement_pre_step
    directly with the augmented inputs: the result must NOT be
    'no_papers_in_inputs' — the papers made it through the paper-list guard.
    """
    import sqlite3
    from optomind_research.runtime.staged_article_completion import (
        visual_procurement_pre_step,
    )

    # Minimal handoff section with one OA source — simulates handoff_data content
    handoff_sources = [
        {
            "paper_id": "oa_candidate_001",
            "title": "Optical metasurface design principles",
            "oa_url": "https://example.org/paper/001.pdf",
        }
    ]

    # Simulate what _visual_remount_live_provider does: extract papers from
    # handoff sections when stage_inputs carries no papers.
    seen: set = set()
    hf_papers: list = []
    fake_handoff = {"sections": [{"sources": handoff_sources}]}
    for section in (fake_handoff.get("sections") or []):
        for src in (section.get("sources") or []):
            pid = str(src.get("paper_id") or "")
            if pid and pid not in seen:
                seen.add(pid)
                hf_papers.append(dict(src))

    assert hf_papers, "Paper extraction from handoff produced nothing"

    # Provide a real (empty) SQLite so the function advances past kb_sqlite guard.
    kb_sqlite = tmp_path / "kb.sqlite"
    sqlite3.connect(str(kb_sqlite)).close()

    augmented = {
        "papers": hf_papers,
        "kb_sqlite": str(kb_sqlite),
    }
    result = visual_procurement_pre_step(augmented, work_dir=tmp_path)

    # The key invariant: we must NOT get no_papers_in_inputs.
    # (The manifest may still be fail_open for other offline reasons, which is fine.)
    assert result.get("reason") != "no_papers_in_inputs", (
        f"Papers from handoff were not forwarded; got reason={result.get('reason')!r}"
    )
    assert result.get("fail_open") is True, "visual_procurement_pre_step must be fail-open"


# Fix 5 — PUBLICATION_METADATA.json artifact contains article_title
# ---------------------------------------------------------------------------

def test_publication_metadata_json_written_with_article_title(tmp_path: Path) -> None:
    """PUBLICATION_METADATA.json is written into staged_completion dir with article_title."""
    import json as _json
    from optomind_research.runtime.publication_mainline_adapter import (
        run_publication_mainline,
    )

    project_root, authoring_root, blueprint = _single_section_fixture(
        tmp_path, "metadata-pkg"
    )
    output_root = project_root / "publication_mainline"
    result = run_publication_mainline(
        project_root=project_root,
        authoring_work_dir=authoring_root,
        output_root=output_root,
        admitted_section_ids=["S01"],
        blueprint=blueprint,
        run_id="metadata-pkg-run",
        enhancement_live=False,
        commander_live=False,
        commander_role_provider=_CommanderProvider(),
        staged_live=False,
    )

    # This fixture intentionally stops at an early enhancement failure, so
    # publication metadata is not materialized. The fail-open summary must
    # still explicitly close the delivery gate.
    assert result.summary["delivery_gate"] == "closed"
    assert result.summary.get("metadata_path", "") == ""


def test_final_citation_map_uses_catalog_before_quality_evaluation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publication metadata must enrich the final map before quality reads it."""
    from optomind_research.runtime import review_harness_orchestrator as module

    query_plan = tmp_path / "query.json"
    query_plan.write_text(json.dumps({"output": {}}), encoding="utf-8")
    kb_path = tmp_path / "kb.sqlite"
    kb_path.write_bytes(b"")
    run_dir = tmp_path / "run"
    review_path = run_dir / "authoring" / "full_review" / "FINAL_REVIEW_EN.md"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text("# Review\n\n[REF:X01]", encoding="utf-8")
    visual_path = run_dir / "visual" / "VISUAL_EDITORIAL_PLAN.json"
    visual_path.parent.mkdir(parents=True, exist_ok=True)
    visual_path.write_text(json.dumps({"placements": []}), encoding="utf-8")
    handoff_path = (
        run_dir
        / "publication_mainline"
        / "handoff"
        / "UNIFIED_MANUSCRIPT_HANDOFF.json"
    )
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text("{}", encoding="utf-8")

    events: list[str] = []
    catalog_path = run_dir / "publication" / "metadata" / "PUBLICATION_METADATA_CATALOG.json"

    def fake_metadata_builder(**kwargs: object) -> dict[str, object]:
        events.append("metadata")
        assert kwargs["staged_manuscript_path"] == review_path
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        catalog_path.write_text(
            json.dumps({"entries": [{"aliases": ["X01"], "doi": "10.1000/x"}]}),
            encoding="utf-8",
        )
        audit_path = catalog_path.with_name("METADATA_AUDIT.json")
        audit_path.write_text("{}", encoding="utf-8")
        return {
            "output_paths": {
                "catalog": str(catalog_path),
                "audit": str(audit_path),
            }
        }

    original_final_map_builder = module.build_final_citation_map

    def recording_final_map_builder(**kwargs: object) -> dict[str, object]:
        events.append("final_map")
        assert kwargs["metadata_catalog_path"] == catalog_path
        return original_final_map_builder(**kwargs)

    def fake_quality_evaluator(**kwargs: object) -> dict[str, object]:
        events.append("quality")
        final_map = json.loads(
            Path(str(kwargs["citation_map_path"])).read_text(encoding="utf-8")
        )
        assert final_map["citations"][0]["citation_identity"] == "doi:10.1000/x"
        return {"status": "passed", "metrics": {}}

    def fake_latex_builder(**_kwargs: object) -> dict[str, object]:
        return {"status": "compiled_awaiting_metadata", "artifacts": {}}

    import optomind_research.runtime.publication_metadata_resolver as resolver_module

    monkeypatch.setattr(
        resolver_module,
        "build_publication_metadata_catalog",
        fake_metadata_builder,
    )
    monkeypatch.setattr(
        module,
        "build_final_citation_map",
        recording_final_map_builder,
    )
    monkeypatch.setattr(module, "evaluate_review_content", fake_quality_evaluator)
    monkeypatch.setattr(module, "build_latex_publication", fake_latex_builder)

    orchestrator = module.ReviewHarnessOrchestrator(
        module.ReviewHarnessConfig(
            query_plan_path=query_plan,
            base_kb_sqlite=kb_path,
            output_root=tmp_path,
            produce_latex_publication=True,
        ),
        run_dir=run_dir,
    )
    orchestrator._finish("completed", "packaging", review_path, visual_path)

    assert events == ["metadata", "final_map", "quality"]


def test_research_plan_quality_uses_only_generated_final_citation_map() -> None:
    """The pre-packaging research-plan branch cannot leak the authoring map."""
    from optomind_research.runtime.review_harness_orchestrator import (
        ReviewHarnessOrchestrator,
    )

    source = inspect.getsource(ReviewHarnessOrchestrator._run_impl)
    branch_start = source.index("if self.config.produce_research_plan:")
    branch_end = source.index("plan_remaining =", branch_start)
    research_branch = source[branch_start:branch_end]

    assert 'research_citation_map_path = self.work_dir / "FINAL_CITATION_MAP.json"' in research_branch
    assert "citation_map_path=research_citation_map_path" in research_branch
    assert "FULL_REVIEW_CITATION_MAP.json" not in research_branch
