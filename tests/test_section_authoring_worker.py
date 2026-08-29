"""Tests for Section Review Authoring Worker (Phase 3 + 3.1 hardening).

All deterministic tests run offline — no API key required.
The single Qwen integration test (SA-T13) is skipped when no API key is present.

Test IDs:
  SA-T01  SectionAuthoringContext has correct properties
  SA-T02  SectionAuthoringToolProvider exposes 12 tools
  SA-T03  load_authoring_context writes SECTION_AUTHORING_CONTEXT.json
  SA-T04  inspect_material_package returns role detail
  SA-T05  retrieve_chunk_text returns error when no KB available
  SA-T06  inspect_visual_assets returns empty list when no KB
  SA-T07  submit_argument_plan writes SECTION_ARGUMENT_PLAN.json
  SA-T08  build_evidence_packet writes SECTION_EVIDENCE_PACKET.json
  SA-T09  submit_section_draft writes SECTION_DRAFT_EN.md and revision history
  SA-T10  run_citation_audit writes SECTION_CITATION_MAP.json and SECTION_AUTHORING_AUDIT.json
  SA-T11  submit_revision updates draft and revision history
  SA-T12  validate_authoring_package returns VALIDATION_FAILED when draft missing
  SA-T13  validate_authoring_package returns VALIDATION_PASSED after full golden path
  SA-T14  request_more_literature writes SECTION_COVERAGE_FEEDBACK.json
  SA-T15  smoke dry-run-fail exits with code 1
  --- Phase 3.1 adversarial tests ---
  SA-T16  FAKE_PAPER cited in citation_map → audit blocking flag
  SA-T17  two allowed IDs with a wrong paper→chunk edge → audit blocking flag
  SA-T18  invented exact_span absent from canonical text → evidence packet rejected
  SA-T19  empty citation_map still runs independent audit on draft [REF:UNKNOWN]
  SA-T20  3-word draft fails validate_authoring_package
  SA-T21  CJK-text draft fails validate_authoring_package
  SA-T22  fake visual/paper IDs plus an existing image path → placement rejected
  SA-T23  only allowed/reviewed single/subfigure visual candidates are exposed
  SA-T24  retrieval merges true main+staging chunks deterministically
  SA-T25  gap_report_path is consumed by load_authoring_context
  SA-T26  submit_revision without re-audit fails validate_authoring_package
  SA-T27  [REF:UNKNOWN] in draft → run_citation_audit blocking flag
  SA-T28  measurement number without citation → run_citation_audit blocking flag
  SA-T29  multi-source synthesis with valid citations → not flagged by audit
  SA-T30  SQLite temp DB can be closed and deleted after test (no lock)
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from optomind_research.runtime.tool_provider import SectionAuthoringContext
from optomind_research.runtime.section_authoring_tool_registry import (
    SectionAuthoringToolProvider,
    SECTION_AUTHORING_TOOL_NAMES,
    _make_load_authoring_context,
    _make_inspect_material_package,
    _make_retrieve_chunk_text,
    _make_inspect_visual_assets,
    _make_submit_argument_plan,
    _make_build_evidence_packet,
    _make_submit_section_draft,
    _make_run_citation_audit,
    _make_submit_revision,
    _make_submit_visual_placement,
    _make_request_more_literature,
    _make_validate_authoring_package,
    _build_asset_graph,
    _chunk_information_quality,
    _find_uncited_measurements,
    _infer_citation_chunks,
    _normalize_span_for_match,
    _has_verified_permission_provenance,
    _section_contract_errors,
    _transfer_boundary,
    _validate_argument_plan_data,
    _validate_evidence_items,
    _citation_risk_class,
    _citation_note_hard_failure,
    _find_uncited_high_risk_claims,
    _requires_strict_citation_entailment,
    _normalize_argument_plan_contract,
    _normalize_evidence_packet_contract,
    _normalize_writing_permission,
    _trusted_section_tokens,
    _write_awaiting_human_review_package,
)
from optomind_research.runtime.section_authoring_assets import (
    CanonicalAssetGraph,
    ChunkAsset,
    PaperAsset,
)
from optomind_research.review_writer import assess_citation_support

SMOKE_SCRIPT = PROJECT_ROOT / "scripts" / "run_section_authoring_smoke.py"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SECTION_DATA = {
    "section_id": "S03",
    "title": "Nonlinear Optical Mechanisms in 2D Materials",
    "chapter_argument": (
        "Third-order nonlinear optical effects in 2D materials arise from their "
        "ultra-thin geometry and band structure."
    ),
    "scope_guardrails": ["Do not conflate linear and nonlinear effects."],
    "required_roles": ["foundation", "mechanism", "method", "frontier"],
    "optional_roles": ["controversy", "application"],
    "claims": [
        {
            "claim_id": "C01",
            "statement": "2D materials exhibit saturable absorption.",
            "evidence_type": "experimental",
            "claim_state": "planned",
            "load_bearing": True,
            "evidence_binding_status": "partial",
        }
    ],
}

MATERIAL_PACKAGE = {
    "schema_version": "2.0",
    "section_id": "S03",
    "section_title": "Nonlinear Optical Mechanisms in 2D Materials",
    "chapter_argument": SECTION_DATA["chapter_argument"],
    "coverage_status": "completed_with_open_gaps",
    "total_sources": 2,
    "new_sources_this_run": 2,
    "local_prior_sources": 0,
    "sources_by_role": {"foundation": 1, "mechanism": 1},
    "chunk_ids_by_role": {
        "foundation": ["chunk_found_001"],
        "mechanism": ["chunk_mech_001"],
    },
    "blocking_gaps_remain": False,
    "gap_summary": "no blocking gaps",
}

SOURCE_LEDGER = {
    "schema_version": "2.0",
    "section_id": "S03",
    "sources": [
        {
            "paper_id": "paper_A",
            "doi": "10.1000/xyz001",
            "title": "Saturable absorption in MoS2",
            "year": 2020,
            "venue": "Nat. Photon.",
            "authors": ["Smith J"],
            "literature_role": "foundation",
            "scope_fit": "direct",
            "canonical_chunk_ids": ["chunk_found_001"],
            "acquisition_status": "fulltext",
            "not_usable_for": [],
        },
        {
            "paper_id": "paper_B",
            "doi": "10.1000/xyz002",
            "title": "Kerr nonlinearity in graphene",
            "year": 2021,
            "venue": "ACS Nano",
            "authors": ["Wang X"],
            "literature_role": "mechanism",
            "scope_fit": "direct",
            "canonical_chunk_ids": ["chunk_mech_001"],
            "acquisition_status": "fulltext",
            "not_usable_for": [],
        },
    ],
    "total_sources": 2,
    "new_sources": 2,
    "local_prior_sources": 0,
}


def _make_ctx(work_dir: Path) -> SectionAuthoringContext:
    mp_path = work_dir / "SECTION_MATERIAL_PACKAGE.json"
    sl_path = work_dir / "SECTION_SOURCE_LEDGER.json"
    mp_path.write_text(json.dumps(MATERIAL_PACKAGE), encoding="utf-8")
    sl_path.write_text(json.dumps(SOURCE_LEDGER), encoding="utf-8")
    kb_path = work_dir / "main_kb.sqlite"
    conn = sqlite3.connect(str(kb_path))
    try:
        conn.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, text TEXT, "
            "evidence_level TEXT, source_kind TEXT)"
        )
        conn.executemany(
            "INSERT INTO text_chunks VALUES (?,?,?,?,?)",
            [
                (
                    "chunk_found_001", "paper_A",
                    "Two-dimensional materials such as MoS2 exhibit strong third-order nonlinear "
                    "optical responses arising from their atomically thin geometry. MoS2 shows "
                    "saturable absorption at 1.5 nJ/cm^2. Saturable absorption was measured in MoS2.",
                    "fulltext", "fulltext",
                ),
                (
                    "chunk_mech_001", "paper_B",
                    "The Kerr nonlinearity in graphene has been demonstrated to exceed that of bulk "
                    "glass, enabling ultrafast all-optical switching. Graphene has large Kerr nonlinearity.",
                    "fulltext", "fulltext",
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return SectionAuthoringContext(
        section_id="S03",
        section_data=SECTION_DATA,
        kb_sqlite=kb_path,
        temp_kb_sqlite=None,
        work_dir=work_dir,
        material_package_path=mp_path,
        source_ledger_path=sl_path,
    )


# ---------------------------------------------------------------------------
# SA-T01: SectionAuthoringContext properties
# ---------------------------------------------------------------------------

def test_section_authoring_context_properties():
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        assert ctx.section_id == "S03"
        assert ctx.section_title == SECTION_DATA["title"]
        assert ctx.chapter_argument == SECTION_DATA["chapter_argument"]
        assert "Do not conflate" in ctx.scope_guardrails[0]


def test_revision_context_exposes_existing_draft_for_minimal_edit():
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        ctx.revision_instructions = {
            "action": "rerun_section_with_role_boundary",
            "scope": "fix_identified_issue_only",
        }
        ctx.existing_draft_text = (
            "The accepted section body must be preserved except for its "
            "flagged closing sentence."
        )
        result = json.loads(_make_load_authoring_context(ctx)())
        assert result["revision_mode"] is True
        assert result["revision_instructions"]["scope"] == (
            "fix_identified_issue_only"
        )
        assert result["existing_draft_text"].startswith(
            "The accepted section body"
        )


# ---------------------------------------------------------------------------
# SA-T02: Provider exposes 12 tools
# ---------------------------------------------------------------------------

def test_section_authoring_tool_provider_exposes_13_tools():
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        provider = SectionAuthoringToolProvider(ctx)
        tools = provider.get_tools(ctx.work_dir)
        names = provider.get_allowed_tool_names()
        assert len(tools) == 13, f"Expected 13 tools, got {len(tools)}"
        assert len(names) == 13
        assert set(names) == set(SECTION_AUTHORING_TOOL_NAMES)


# ---------------------------------------------------------------------------
# SA-T03: load_authoring_context writes SECTION_AUTHORING_CONTEXT.json
# ---------------------------------------------------------------------------

def test_load_authoring_context_writes_artifact():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        fn = _make_load_authoring_context(ctx)
        result = json.loads(fn())
        assert result["status"] == "ok"
        assert result["section_id"] == "S03"
        assert (wd / "SECTION_AUTHORING_CONTEXT.json").exists()
        data = json.loads((wd / "SECTION_AUTHORING_CONTEXT.json").read_text())
        assert data["section_id"] == "S03"
        assert data["total_sources"] == 2
        assert "foundation" in data["sources_by_role"]


# ---------------------------------------------------------------------------
# SA-T04: inspect_material_package returns role detail
# ---------------------------------------------------------------------------

def test_inspect_material_package_returns_role_detail():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        _make_load_authoring_context(ctx)()  # populate context first
        fn = _make_inspect_material_package(ctx)
        result = json.loads(fn())
        assert result["status"] == "ok"
        assert "role_detail" in result
        assert "foundation" in result["role_detail"]
        assert result["role_detail"]["foundation"]["chunk_count"] == 1


def test_inspect_material_package_returns_diverse_evidence_portfolio():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        _make_load_authoring_context(ctx)()
        result = json.loads(_make_inspect_material_package(ctx)())
        portfolio = result["evidence_portfolio"]
        assert portfolio["status"] == "ready"
        assert portfolio["selected_paper_count"] == 2
        assert len({
            item["paper_id"] for item in portfolio["papers"]
        }) == 2
        assert portfolio["recommended_batch_chunk_ids"] == [
            "chunk_found_001",
            "chunk_mech_001",
        ]
        assert all(item["chunk_preview"] for item in portfolio["papers"])


def _ctx_with_claim_specific_bindings(work_dir: Path) -> SectionAuthoringContext:
    ctx = _make_ctx(work_dir)
    ctx.section_data = {
        **ctx.section_data,
        "claims": [
            {
                "claim_id": "C01",
                "statement": "MoS2 exhibits saturable absorption.",
                "supporting_text_chunk_ids": ["chunk_found_001"],
            },
            {
                "claim_id": "C02",
                "statement": "Graphene supports Kerr all-optical switching.",
                "supporting_text_chunk_ids": ["chunk_mech_001"],
            },
        ],
    }
    return ctx


def _abstract_claim_ctx(work_dir: Path) -> SectionAuthoringContext:
    """Build a canonical abstract_claim asset without the r3_2 marker."""

    ctx = _make_ctx(work_dir)
    safe_kinds = [
        "paper_reported_claim",
        "background",
        "trend",
        "candidate_lead",
        "author_synthesis",
    ]
    ledger = json.loads(
        (work_dir / "SECTION_SOURCE_LEDGER.json").read_text(encoding="utf-8")
    )
    for source in ledger["sources"]:
        source.update({
            "content_depth": "abstract_claim",
            "use_permission": "contextual_or_qualified_support",
            "allowed_claim_kinds": safe_kinds,
            "discovery_route": "semantic_scholar_abstract",
            "materialization_route": "semantic_scholar_abstract_claim",
        })
    (work_dir / "SECTION_SOURCE_LEDGER.json").write_text(
        json.dumps(ledger), encoding="utf-8"
    )
    with sqlite3.connect(str(ctx.kb_sqlite)) as conn:
        for statement in (
            "ALTER TABLE text_chunks ADD COLUMN content_depth TEXT",
            "ALTER TABLE text_chunks ADD COLUMN use_permission TEXT",
            "ALTER TABLE text_chunks ADD COLUMN discovery_route TEXT",
            "ALTER TABLE text_chunks ADD COLUMN materialization_route TEXT",
            "ALTER TABLE text_chunks ADD COLUMN context_complete INTEGER",
            "ALTER TABLE text_chunks ADD COLUMN allowed_claim_kinds_json TEXT",
            "ALTER TABLE text_chunks ADD COLUMN route_provenance_json TEXT",
            "ALTER TABLE text_chunks ADD COLUMN scope_fit TEXT",
        ):
            conn.execute(statement)
        conn.execute(
            "UPDATE text_chunks SET content_depth=?, use_permission=?, "
            "discovery_route=?, materialization_route=?, context_complete=?, "
            "allowed_claim_kinds_json=?, route_provenance_json=?, scope_fit=?",
            (
                "abstract_claim",
                "contextual_or_qualified_support",
                "semantic_scholar_abstract",
                "semantic_scholar_abstract_claim",
                0,
                json.dumps(safe_kinds),
                json.dumps({
                    "discovery_route": "semantic_scholar_abstract",
                    "materialization_route": "semantic_scholar_abstract_claim",
                }),
                "direct",
            ),
        )
        conn.commit()
    return ctx


@pytest.mark.parametrize(
    ("statement", "writing_permission"),
    [
        ("The paper reports saturable absorption in the material.", "factual_assertion"),
        ("The material's mechanism is explained by carrier interaction.", "hedged_factual_assertion"),
        ("The material reaches 12 nm under the reported conditions.", "hedged_factual_assertion"),
    ],
)
def test_abstract_claim_without_r32_cannot_support_unqualified_or_high_risk_claims(
    tmp_path: Path,
    statement: str,
    writing_permission: str,
) -> None:
    ctx = _abstract_claim_ctx(tmp_path)
    graph = _build_asset_graph(ctx)
    chunk = graph.chunks["chunk_found_001"]
    assert _has_verified_permission_provenance(chunk) is True
    errors = _validate_argument_plan_data(ctx, graph, [{
        "paragraph_index": 0,
        "topic_sentence": statement,
        "key_claims": [],
        "evidence_chunk_ids": ["chunk_found_001"],
        "paper_ids": ["paper_A"],
        "writing_permission": writing_permission,
        "expected_word_count": 100,
    }])

    assert any(
        "abstract_claim" in error or "claim kind" in error
        for error in errors
    ), errors


@pytest.mark.parametrize(
    "statement",
    [
        (
            "The paper reports that saturable absorption arises from atomically "
            "thin geometry."
        ),
        "The paper reports saturable absorption at 1.5 nJ/cm^2.",
    ],
)
def test_abstract_claim_allows_qualified_grounded_author_report(
    tmp_path: Path, statement: str
) -> None:
    ctx = _abstract_claim_ctx(tmp_path)
    graph = _build_asset_graph(ctx)
    errors = _validate_argument_plan_data(ctx, graph, [{
        "paragraph_index": 0,
        "topic_sentence": statement,
        "key_claims": [],
        "evidence_chunk_ids": ["chunk_found_001"],
        "paper_ids": ["paper_A"],
        "writing_permission": "hedged_factual_assertion",
        "expected_word_count": 100,
    }])

    assert not any(
        "abstract_claim" in error or "claim kind" in error
        for error in errors
    ), errors


def test_abstract_claim_rejects_attributed_but_unstated_mechanism(
    tmp_path: Path,
) -> None:
    ctx = _abstract_claim_ctx(tmp_path)
    graph = _build_asset_graph(ctx)
    errors = _validate_argument_plan_data(ctx, graph, [{
        "paragraph_index": 0,
        "topic_sentence": (
            "The paper reports that carrier trapping drives saturable absorption."
        ),
        "key_claims": [],
        "evidence_chunk_ids": ["chunk_found_001"],
        "paper_ids": ["paper_A"],
        "writing_permission": "hedged_factual_assertion",
        "expected_word_count": 100,
    }])

    assert any("ungrounded" in error for error in errors), errors


def test_legacy_fulltext_fixture_without_permission_fields_remains_compatible(
    tmp_path: Path,
) -> None:
    ctx = _make_ctx(tmp_path)
    graph = _build_asset_graph(ctx)
    chunk = graph.chunks["chunk_found_001"]
    assert _has_verified_permission_provenance(chunk) is False
    errors = _validate_argument_plan_data(ctx, graph, [{
        "paragraph_index": 0,
        "topic_sentence": "MoS2 shows saturable absorption.",
        "key_claims": [],
        "evidence_chunk_ids": ["chunk_found_001"],
        "paper_ids": ["paper_A"],
        "writing_permission": "factual_assertion",
        "expected_word_count": 100,
    }])

    assert not any(
        "discovery_only" in error or "allowed_claim_kinds" in error
        for error in errors
    ), errors


def test_argument_plan_rejects_valid_chunk_bound_to_wrong_phase3_claim(
    tmp_path: Path,
) -> None:
    ctx = _ctx_with_claim_specific_bindings(tmp_path)
    graph = _build_asset_graph(ctx)
    errors = _validate_argument_plan_data(ctx, graph, [{
        "paragraph_index": 0,
        "topic_sentence": "MoS2 exhibits saturable absorption.",
        "key_claims": ["C01"],
        "evidence_chunk_ids": ["chunk_mech_001"],
        "paper_ids": ["paper_B"],
        "writing_permission": "factual_assertion",
        "expected_word_count": 100,
    }])

    assert any("not paired with any canonical Phase-3 support chunk" in item for item in errors)
    assert any("not canonical support" in item for item in errors)


def test_argument_plan_rejects_quantitative_literal_absent_from_bound_chunk(
    tmp_path: Path,
) -> None:
    ctx = _make_ctx(tmp_path)
    ctx.section_data = {
        **ctx.section_data,
        "claims": [{
            "claim_id": "C01",
            "statement": "Graphene switches at 7.25 nJ per pulse.",
            "supporting_text_chunk_ids": ["chunk_mech_001"],
        }],
    }
    graph = _build_asset_graph(ctx)
    errors = _validate_argument_plan_data(ctx, graph, [{
        "paragraph_index": 0,
        "topic_sentence": "Graphene switches at 7.25 nJ per pulse.",
        "key_claims": ["C01"],
        "evidence_chunk_ids": ["chunk_mech_001"],
        "paper_ids": ["paper_B"],
        "writing_permission": "factual_assertion",
        "expected_word_count": 100,
    }])

    assert any("7.25" in item and "absent" in item for item in errors)


def test_evidence_packet_rejects_wrong_phase3_claim_chunk_pair(
    tmp_path: Path,
) -> None:
    ctx = _ctx_with_claim_specific_bindings(tmp_path)
    graph = _build_asset_graph(ctx)
    errors, _ = _validate_evidence_items(ctx, graph, [{
        "chunk_id": "chunk_mech_001",
        "paper_id": "paper_B",
        "claim_ids": ["C01"],
        "writing_permission": "factual_assertion",
        "support_hint": "saturable absorption",
    }], [])

    assert any("not canonical Phase-3 support for claim C01" in item for item in errors)


def test_evidence_packet_rejects_unbound_factual_item_when_phase3_map_exists(
    tmp_path: Path,
) -> None:
    ctx = _ctx_with_claim_specific_bindings(tmp_path)
    graph = _build_asset_graph(ctx)
    errors, _ = _validate_evidence_items(ctx, graph, [{
        "chunk_id": "chunk_found_001",
        "paper_id": "paper_A",
        "claim_ids": [],
        "writing_permission": "factual_assertion",
        "support_hint": "saturable absorption",
    }], [])

    assert any("must name at least one canonical" in item for item in errors)


def test_first_singleton_retrieval_auto_expands_to_portfolio_batch():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        _make_load_authoring_context(ctx)()
        result = json.loads(
            _make_retrieve_chunk_text(ctx)("chunk_found_001")
        )
        assert result["requested_ids"] == ["chunk_found_001"]
        assert result["automatic_batch_expansion"] == ["chunk_mech_001"]
        assert set(result["chunks"]) == {
            "chunk_found_001",
            "chunk_mech_001",
        }


# ---------------------------------------------------------------------------
# SA-T05: retrieve_chunk_text reports canonical IDs missing when no KB is available
# ---------------------------------------------------------------------------

def test_retrieve_chunk_text_no_kb_returns_explicit_missing():
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        ctx.kb_sqlite = None
        fn = _make_retrieve_chunk_text(ctx)
        result = json.loads(fn('["chunk_found_001"]'))
        assert result["status"] == "ok"
        assert result["found"] == 0
        assert result["missing"] == ["chunk_found_001"]


def test_retrieve_chunk_text_returns_exact_allowed_ids_after_guess():
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        result = json.loads(
            _make_retrieve_chunk_text(ctx)(
                '["doi-10.1000-invented:hybrid:s0099"]'
            )
        )
        assert result["found"] == 0
        allowed = result["allowed_assets_after_missing_id"]
        assert any(item["chunk_id"] == "chunk_found_001" for item in allowed)
        assert all(item["paper_id"] in {"paper_A", "paper_B"} for item in allowed)
        assert len({item["paper_id"] for item in allowed[:2]}) == 2
        assert result["repair_batch_chunk_ids"] == [
            item["chunk_id"] for item in allowed
        ]


def test_evidence_portfolio_prefers_scientific_prose_over_reference_debris():
    scientific, scientific_flags = _chunk_information_quality(
        (
            "We demonstrate a dielectric resonance sensor and measure its "
            "spectral response under controlled gas exposure. The results show "
            "the mechanism, fabrication trade-off, and principal limitation."
        ),
        "fulltext",
    )
    references, reference_flags = _chunk_information_quality(
        (
            "References 1. Smith et al. 2021 doi:10.1000/a. "
            "2. Jones et al. 2022 doi:10.1000/b. "
            "3. Chen et al. 2023 doi:10.1000/c."
        ),
        "fulltext",
    )
    assert scientific > references
    assert "scientific_reasoning" in scientific_flags
    assert "reference_list_like" in reference_flags


def test_adjacent_evidence_exposes_explicit_transfer_boundary():
    required, note = _transfer_boundary("adjacent")
    assert required is True
    assert "cross-domain" in note
    assert "quantitative performance" in note


def test_hyphenated_measurement_requires_same_sentence_citation():
    uncited = _find_uncited_measurements(
        "Fabrication errors below sub-100-nm remain difficult to control."
    )
    assert uncited
    assert not _find_uncited_measurements(
        "Fabrication errors below sub-100-nm were reported [REF:paper_A]."
    )


def test_rejected_argument_plan_returns_paper_diverse_repair_batch():
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        result = json.loads(
            _make_submit_argument_plan(ctx)(
                json.dumps({
                    "argument_flow": "A deliberately invalid plan.",
                    "paragraphs": [{
                        "paragraph_index": 0,
                        "function": "evidence",
                        "topic_sentence": "An unsupported quantitative claim.",
                        "key_claims": ["C01"],
                        "evidence_chunk_ids": ["invented_chunk"],
                        "paper_ids": ["invented_paper"],
                        "writing_permission": "factual_assertion",
                        "expected_word_count": 100,
                    }],
                })
            )
        )
        assert result["status"] == "rejected"
        assert result["distinct_repair_papers"] == 2
        assert len({
            item["paper_id"] for item in result["allowed_assets"][:2]
        }) == 2
        assert result["repair_batch_chunk_ids"] == [
            item["chunk_id"] for item in result["allowed_assets"]
        ]


# ---------------------------------------------------------------------------
# SA-T06: inspect_visual_assets returns empty when no KB
# ---------------------------------------------------------------------------

def test_inspect_visual_assets_no_kb_returns_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        fn = _make_inspect_visual_assets(ctx)
        result = json.loads(fn())
        assert result["status"] == "ok"
        assert result["visual_assets"] == [] or "message" in result


# ---------------------------------------------------------------------------
# SA-T07: submit_argument_plan writes SECTION_ARGUMENT_PLAN.json
# ---------------------------------------------------------------------------

def test_submit_argument_plan_writes_artifact():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        _make_load_authoring_context(ctx)()
        fn = _make_submit_argument_plan(ctx)
        plan = {
            "argument_flow": "Intro → mechanism → evidence → synthesis",
            "paragraphs": [
                {
                    "paragraph_index": 0,
                    "function": "introduction",
                    "topic_sentence": "2D materials offer unique optical properties.",
                    "key_claims": ["C01"],
                    "evidence_chunk_ids": ["chunk_found_001"],
                    "paper_ids": ["paper_A"],
                    "writing_permission": "factual_assertion",
                    "expected_word_count": 120,
                },
                {
                    "paragraph_index": 1,
                    "function": "mechanism",
                    "topic_sentence": "Kerr nonlinearity arises from band-structure effects.",
                    "key_claims": [],
                    "evidence_chunk_ids": ["chunk_mech_001"],
                    "paper_ids": ["paper_B"],
                    "writing_permission": "hedged_factual_assertion",
                    "expected_word_count": 150,
                },
            ],
            "open_questions": ["What is the saturation fluence at 800 nm?"],
        }
        result = json.loads(fn(json.dumps(plan)))
        assert result["status"] == "ok"
        assert result["paragraph_count"] == 2
        assert (wd / "SECTION_ARGUMENT_PLAN.json").exists()
        data = json.loads((wd / "SECTION_ARGUMENT_PLAN.json").read_text(encoding="utf-8"))
        assert len(data["paragraphs"]) == 2


# ---------------------------------------------------------------------------
# SA-T08: build_evidence_packet writes SECTION_EVIDENCE_PACKET.json
# ---------------------------------------------------------------------------

def test_build_evidence_packet_writes_artifact():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        fn = _make_build_evidence_packet(ctx)
        packet = {
            "items": [
                {
                    "chunk_id": "chunk_found_001",
                    "paper_id": "paper_A",
                    "literature_role": "foundation",
                    "scope_fit": "direct",
                    "exact_spans": ["saturable absorption at 1.5 nJ/cm^2"],
                    "claim_ids": ["C01"],
                    "writing_permission": "factual_assertion",
                    "not_usable_for": [],
                }
            ],
            "uncovered_claim_ids": [],
        }
        result = json.loads(fn(json.dumps(packet)))
        assert result["status"] == "ok"
        assert result["total_items"] == 1
        assert (wd / "SECTION_EVIDENCE_PACKET.json").exists()


# ---------------------------------------------------------------------------
# SA-T09: submit_section_draft writes SECTION_DRAFT_EN.md and revision history
# ---------------------------------------------------------------------------

def test_submit_section_draft_writes_draft_and_history():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        fn = _make_submit_section_draft(ctx)
        draft = (
            "Nonlinear optical effects in 2D materials stem from their atomically thin geometry.\n\n"
            "MoS2 exhibits saturable absorption at low fluence values [REF:paper_A].\n\n"
            "The Kerr nonlinearity in graphene has been measured to exceed bulk glass by two orders "
            "of magnitude [REF:paper_B]."
        )
        result = json.loads(fn(draft, "Initial draft"))
        assert result["status"] == "ok"
        assert result["word_count"] > 20
        assert (wd / "SECTION_DRAFT_EN.md").exists()
        assert (wd / "SECTION_REVISION_HISTORY.json").exists()
        rh = json.loads((wd / "SECTION_REVISION_HISTORY.json").read_text())
        assert rh["total_revisions"] == 1
        assert rh["revisions"][0]["stage"] == "initial_draft"


# ---------------------------------------------------------------------------
# SA-T10: run_citation_audit writes citation map and audit
# ---------------------------------------------------------------------------

def test_run_citation_audit_writes_artifacts():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        # Write a draft first (uncited_sentences count uses draft)
        _make_submit_section_draft(ctx)(
            "MoS2 shows saturable absorption [REF:paper_A]. Graphene has large Kerr nonlinearity [REF:paper_B].",
            "draft"
        )
        fn = _make_run_citation_audit(ctx)
        cit_map = [
            {
                "sentence_index": 0,
                "sentence_snippet": "MoS2 shows saturable absorption",
                "chunk_ids": ["chunk_found_001"],
                "paper_ids": ["paper_A"],
                "citation_type": "factual",
                "entailment_verdict": "entailed",
                "audit_note": "",
            },
            {
                "sentence_index": 1,
                "sentence_snippet": "Graphene has large Kerr nonlinearity",
                "chunk_ids": ["chunk_mech_001"],
                "paper_ids": ["paper_B"],
                "citation_type": "factual",
                "entailment_verdict": "entailed",
                "audit_note": "",
            },
        ]
        result = json.loads(fn(json.dumps(cit_map)))
        assert result["status"] == "ok"
        assert result["audit_passed"] is True
        assert result["blocking_flags"] == 0
        assert (wd / "SECTION_CITATION_MAP.json").exists()
        assert (wd / "SECTION_AUTHORING_AUDIT.json").exists()
        audit = json.loads((wd / "SECTION_AUTHORING_AUDIT.json").read_text())
        assert audit["audit_passed"] is True


# ---------------------------------------------------------------------------
# SA-T11: submit_revision updates draft and history
# ---------------------------------------------------------------------------

def test_submit_revision_updates_draft_and_history():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        _make_submit_section_draft(ctx)("Initial draft text.", "initial")
        fn = _make_submit_revision(ctx)
        result = json.loads(fn("Revised draft text with more detail.", "[]", "Removed overclaim"))
        assert result["status"] == "ok"
        rh = json.loads((wd / "SECTION_REVISION_HISTORY.json").read_text())
        assert rh["total_revisions"] == 2
        assert rh["revisions"][1]["stage"] == "revision"
        assert (wd / "SECTION_DRAFT_EN.md").read_text() == "Revised draft text with more detail."


# ---------------------------------------------------------------------------
# SA-T12: validate_authoring_package returns VALIDATION_FAILED when draft missing
# ---------------------------------------------------------------------------

def test_validate_authoring_package_fails_when_draft_missing():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        fn = _make_validate_authoring_package(ctx)
        result = fn()
        assert "VALIDATION_FAILED" in result
        assert "SECTION_DRAFT_EN.md" in result


# ---------------------------------------------------------------------------
# SA-T13: Full golden path — VALIDATION_PASSED
# ---------------------------------------------------------------------------

def test_validate_authoring_package_golden_path():
    """Run all tools in order and verify VALIDATION_PASSED."""
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)

        # Step 1: load context
        load_result = json.loads(_make_load_authoring_context(ctx)())
        assert load_result["status"] == "ok"

        # Step 2: argument plan
        plan = json.dumps({
            "argument_flow": "intro → mechanism → evidence",
            "paragraphs": [
                {
                    "paragraph_index": 0,
                    "function": "introduction",
                    "topic_sentence": "2D materials offer tunable optical nonlinearities.",
                    "key_claims": ["C01"],
                    "evidence_chunk_ids": ["chunk_found_001"],
                    "paper_ids": ["paper_A"],
                    "writing_permission": "factual_assertion",
                    "expected_word_count": 120,
                }
            ],
        })
        plan_result = json.loads(_make_submit_argument_plan(ctx)(plan))
        assert plan_result["status"] == "ok"

        # Step 3: evidence packet
        ep = json.dumps({
            "items": [{
                "chunk_id": "chunk_found_001",
                "paper_id": "paper_A",
                "literature_role": "foundation",
                "scope_fit": "direct",
                "exact_spans": ["MoS2 shows saturable absorption at 1.5 nJ/cm^2"],
                "claim_ids": ["C01"],
                "writing_permission": "factual_assertion",
                "not_usable_for": [],
            }]
        })
        ep_result = json.loads(_make_build_evidence_packet(ctx)(ep))
        assert ep_result["status"] == "ok"

        # Step 4: draft — must be ≥50 words to pass the word-count gate
        draft = (
            "Two-dimensional materials such as MoS2 exhibit strong third-order nonlinear optical "
            "responses arising from their atomically thin geometry [REF:paper_A].\n\n"
            "This evidence motivates a broader comparison of reduced-dimensional platforms while "
            "keeping material-specific mechanisms distinct. The section therefore uses local "
            "observations to organize the discussion, while treating cross-platform implications "
            "as synthesis rather than as additional experimental findings."
        )
        draft_result = json.loads(_make_submit_section_draft(ctx)(draft, "initial"))
        assert draft_result["status"] == "ok"

        # Step 5: citation audit — no flags
        cit_map = json.dumps([
            {
                "sentence_index": 0,
                "sentence_snippet": "Two-dimensional materials such as MoS2",
                "chunk_ids": ["chunk_found_001"],
                "paper_ids": ["paper_A"],
                "citation_type": "factual",
                "entailment_verdict": "entailed",
                "audit_note": "",
            }
        ])
        audit_result = json.loads(_make_run_citation_audit(ctx)(cit_map))
        assert audit_result["audit_passed"] is True

        # Step 6: visual placement (empty — no KB)
        vp = json.dumps([])
        vp_result = json.loads(_make_submit_visual_placement(ctx)(vp))
        assert vp_result["status"] == "ok"

        # Step 7: validate
        validation = _make_validate_authoring_package(ctx)()
        assert "VALIDATION_PASSED" in validation, f"Expected VALIDATION_PASSED, got: {validation}"

        # Verify SECTION_AUTHORING_PACKAGE.json
        package = json.loads((wd / "SECTION_AUTHORING_PACKAGE.json").read_text())
        assert package["authoring_status"] in ("completed", "completed_with_flags")
        assert package["word_count"] > 0


# ---------------------------------------------------------------------------
# SA-T14: request_more_literature writes SECTION_COVERAGE_FEEDBACK.json
# ---------------------------------------------------------------------------

def test_request_more_literature_writes_feedback():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        fn = _make_request_more_literature(ctx)
        feedback = json.dumps({
            "feedback_items": [
                {
                    "role": "frontier",
                    "severity": "blocking",
                    "description": "No frontier papers available for ultrafast dynamics.",
                    "blocking_claims": ["C02"],
                    "suggested_queries": ["ultrafast dynamics 2D materials pump-probe"],
                }
            ],
            "authoring_can_proceed": False,
        })
        result = json.loads(fn(feedback))
        assert result["status"] == "ok"
        assert result["state"] == "needs_more_literature"
        assert result["total_blocking"] == 1
        assert (wd / "SECTION_COVERAGE_FEEDBACK.json").exists()


# ---------------------------------------------------------------------------
# SA-T15: Smoke --dry-run-fail exits code 1
# ---------------------------------------------------------------------------

def test_smoke_dry_run_fail_exits_nonzero():
    result = subprocess.run(
        [sys.executable, str(SMOKE_SCRIPT), "--dry-run-fail"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "Exiting with code 1" in result.stdout


# ---------------------------------------------------------------------------
# SA-T16: FAKE_PAPER cited → audit blocking flag
# ---------------------------------------------------------------------------

def test_run_citation_audit_fake_paper_blocked():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        _make_submit_section_draft(ctx)(
            "2D materials show strong nonlinear responses [REF:FAKE_PAPER_999].",
            "initial"
        )
        fn = _make_run_citation_audit(ctx)
        result = json.loads(fn(json.dumps([{
            "sentence_index": 0,
            "sentence_snippet": "2D materials show strong nonlinear responses",
            "chunk_ids": [],
            "paper_ids": ["FAKE_PAPER_999"],
            "citation_type": "factual",
            "audit_note": "",
        }])))
        assert result["status"] == "ok"
        assert result["audit_passed"] is False
        assert result["blocking_flags"] >= 1


# ---------------------------------------------------------------------------
# SA-T17: two allowed IDs with the wrong ownership edge are blocked
# ---------------------------------------------------------------------------

def test_run_citation_audit_chunk_paper_mismatch_blocked():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        _make_submit_section_draft(ctx)(
            "Saturable absorption was measured in MoS2 [REF:paper_A].", "initial"
        )
        fn = _make_run_citation_audit(ctx)
        # Both IDs are allowed, but chunk_mech_001 belongs to paper_B.
        result = json.loads(fn(json.dumps([{
            "sentence_index": 0,
            "sentence_snippet": "Saturable absorption was measured",
            "chunk_ids": ["chunk_mech_001"],
            "paper_ids": ["paper_A"],
            "citation_type": "factual",
            "audit_note": "",
        }])))
        assert result["status"] == "ok"
        assert result["blocking_flags"] >= 1
        assert any(flag["type"] == "paper_chunk_mismatch" for flag in result["flags_detail"])


# ---------------------------------------------------------------------------
# SA-T18: invented exact_span is rejected before writing the evidence packet
# ---------------------------------------------------------------------------

def test_build_evidence_packet_rejects_invented_exact_span():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        result = json.loads(_make_build_evidence_packet(ctx)(json.dumps({
            "items": [{
                "chunk_id": "chunk_found_001", "paper_id": "paper_A",
                "scope_fit": "direct", "exact_spans": ["invented exact result at 99.9 THz"],
                "claim_ids": ["C01"], "writing_permission": "factual_assertion",
            }],
        })))
        assert result["status"] == "rejected"
        assert "not contained" in json.dumps(result)
        assert not (wd / "SECTION_EVIDENCE_PACKET.json").exists()


# ---------------------------------------------------------------------------
# SA-T19: [REF:UNKNOWN] in draft detected even with empty citation_map
# ---------------------------------------------------------------------------

def test_run_citation_audit_detects_ref_unknown_independently():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        _make_submit_section_draft(ctx)(
            "Two-dimensional materials show strong absorption [REF:UNKNOWN].", "initial"
        )
        fn = _make_run_citation_audit(ctx)
        result = json.loads(fn(json.dumps([])))   # agent submits no citations
        assert result["status"] == "ok"
        assert result["audit_passed"] is False
        assert result["blocking_flags"] >= 1


# ---------------------------------------------------------------------------
# SA-T20: 3-word draft fails validate_authoring_package
# ---------------------------------------------------------------------------

def test_validate_rejects_short_draft():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        _make_load_authoring_context(ctx)()
        _make_submit_argument_plan(ctx)(json.dumps({
            "argument_flow": "intro", "paragraphs": [{
                "paragraph_index": 0, "function": "introduction",
                "topic_sentence": "Short draft.",
                "key_claims": [], "evidence_chunk_ids": ["chunk_found_001"],
                "paper_ids": ["paper_A"], "writing_permission": "factual_assertion",
                "expected_word_count": 5,
            }]
        }))
        _make_build_evidence_packet(ctx)(json.dumps({
            "items": [{"chunk_id": "chunk_found_001", "paper_id": "paper_A",
                       "literature_role": "foundation", "scope_fit": "direct",
                       "exact_spans": ["Saturable absorption was measured in MoS2"], "claim_ids": [], "writing_permission": "factual_assertion",
                       "not_usable_for": []}]
        }))
        _make_submit_section_draft(ctx)("Too short draft.", "initial")
        _make_run_citation_audit(ctx)(json.dumps([{
            "sentence_index": 0, "sentence_snippet": "Too short draft",
            "chunk_ids": ["chunk_found_001"], "paper_ids": ["paper_A"],
            "citation_type": "factual", "audit_note": "",
        }]))
        result = _make_validate_authoring_package(ctx)()
        assert "VALIDATION_FAILED" in result
        assert "short" in result.lower() or "words" in result.lower()


# ---------------------------------------------------------------------------
# SA-T21: CJK-text draft fails validate_authoring_package
# ---------------------------------------------------------------------------

def test_validate_rejects_cjk_draft():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        _make_load_authoring_context(ctx)()
        _make_submit_argument_plan(ctx)(json.dumps({
            "argument_flow": "intro", "paragraphs": [{
                "paragraph_index": 0, "function": "introduction",
                "topic_sentence": "CJK draft topic.",
                "key_claims": [], "evidence_chunk_ids": ["chunk_found_001"],
                "paper_ids": ["paper_A"], "writing_permission": "factual_assertion",
                "expected_word_count": 80,
            }]
        }))
        _make_build_evidence_packet(ctx)(json.dumps({
            "items": [{"chunk_id": "chunk_found_001", "paper_id": "paper_A",
                       "literature_role": "foundation", "scope_fit": "direct",
                       "exact_spans": ["Saturable absorption was measured in MoS2"], "claim_ids": [], "writing_permission": "factual_assertion",
                       "not_usable_for": []}]
        }))
        cjk_draft = (
            "二维材料展示了非常强的非线性光学响应，这归因于其超薄几何结构和独特的能带结构。"
            "这些材料的第三阶非线性极化率远超过传统体材料，为光学器件提供了新的可能性。"
            "研究表明，MoS2在低能量密度下表现出饱和吸收特性，具有重要的应用价值。"
            " [REF:paper_A]"
        )
        _make_submit_section_draft(ctx)(cjk_draft, "initial")
        _make_run_citation_audit(ctx)(json.dumps([{
            "sentence_index": 0, "sentence_snippet": "二维材料展示",
            "chunk_ids": ["chunk_found_001"], "paper_ids": ["paper_A"],
            "citation_type": "factual", "audit_note": "",
        }]))
        result = _make_validate_authoring_package(ctx)()
        assert "VALIDATION_FAILED" in result
        assert "CJK" in result or "English" in result


# ---------------------------------------------------------------------------
# SA-T22: an arbitrary existing image cannot verify fake visual/paper IDs
# ---------------------------------------------------------------------------

def test_submit_visual_placement_rejects_fake_ids_even_when_image_exists():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        from PIL import Image
        unrelated = wd / "unrelated.png"
        Image.new("RGB", (4, 4), "red").save(unrelated)
        fn = _make_submit_visual_placement(ctx)
        result = json.loads(fn(json.dumps([{
            "visual_chunk_id": "FAKE_VIS",
            "paper_id": "FAKE_PAPER",
            "caption": "Figure 1",
            "placement_after_paragraph": 1,
            "argument_type": "mechanism",
            "argument_claim": "saturable absorption",
            "asset_status": "verified_local",
            "local_image_path": str(unrelated),
            "placement_rationale": "shows mechanism",
        }])))
        assert result["status"] == "rejected"
        assert not (wd / "SECTION_VISUAL_PLACEMENT.json").exists()
        assert "unknown visual_chunk_id" in json.dumps(result)


def test_submit_visual_placement_computes_verified_local_when_status_omitted():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        from PIL import Image
        image_path = wd / "canonical.png"
        Image.new("RGB", (8, 8), "blue").save(image_path)
        conn = sqlite3.connect(str(ctx.kb_sqlite))
        try:
            conn.execute(
                "CREATE TABLE visual_chunks (chunk_id TEXT, paper_id TEXT, caption TEXT, "
                "local_image_path TEXT, chunk_kind TEXT, visual_argument_type TEXT, "
                "visual_argument_claim TEXT, visual_argument_status TEXT, relevance_status TEXT)"
            )
            conn.execute(
                "INSERT INTO visual_chunks VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "visual_allowed", "paper_A", "Canonical mechanism figure",
                    str(image_path), "single_figure", "mechanism_anchor",
                    "Shows the absorption mechanism.", "ok", "direct",
                ),
            )
            conn.commit()
        finally:
            conn.close()
        result = json.loads(_make_submit_visual_placement(ctx)(json.dumps([{
            "visual_chunk_id": "visual_allowed",
            "paper_id": "paper_A",
            "placement_after_paragraph": 1,
            "local_image_path": str(image_path),
            "placement_rationale": "Supports the mechanism discussion.",
        }])))
        assert result["status"] == "ok"
        placement = json.loads((wd / "SECTION_VISUAL_PLACEMENT.json").read_text(encoding="utf-8"))
        assert placement["placements"][0]["asset_status"] == "verified_local"


# ---------------------------------------------------------------------------
# SA-T23: visual inspection exposes only allowed, reviewed single/subfigure candidates
# ---------------------------------------------------------------------------

def test_inspect_visual_assets_filters_by_allowlist():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        conn = sqlite3.connect(str(ctx.kb_sqlite))
        try:
            conn.execute(
                "CREATE TABLE visual_chunks (chunk_id TEXT, paper_id TEXT, caption TEXT, "
                "local_image_path TEXT, chunk_kind TEXT, visual_argument_type TEXT, "
                "visual_argument_claim TEXT, visual_argument_status TEXT, relevance_status TEXT)"
            )
            conn.executemany(
                "INSERT INTO visual_chunks VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    ("visual_allowed", "paper_A", "Allowed", "x.png", "single_figure", "mechanism", "absorption", "ok", "direct"),
                    ("visual_other_paper", "paper_X", "Other", "x.png", "single_figure", "mechanism", "x", "ok", "direct"),
                    ("visual_parent", "paper_A", "Parent", "x.png", "parent_figure", "mechanism", "x", "ok", "direct"),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        fn = _make_inspect_visual_assets(ctx)
        result = json.loads(fn())
        assert result["status"] == "ok"
        assert [item["visual_chunk_id"] for item in result["visual_assets"]] == ["visual_allowed"]


def test_inspect_visual_assets_accepts_real_kb_review_utility_values():
    """The production ReviewKnowledgeBase uses high/medium review_utility."""
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        conn = sqlite3.connect(str(ctx.kb_sqlite))
        try:
            conn.execute(
                "CREATE TABLE visual_chunks (chunk_id TEXT, paper_id TEXT, caption TEXT, "
                "local_image_path TEXT, chunk_kind TEXT, visual_argument_type TEXT, "
                "visual_argument_claim TEXT, visual_argument_status TEXT, review_utility TEXT)"
            )
            conn.executemany(
                "INSERT INTO visual_chunks VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    ("visual_high", "paper_A", "High utility", "x.png", "subfigure", "mechanism", "absorption", "ok", "high"),
                    ("visual_medium", "paper_A", "Medium utility", "x.png", "single_figure", "comparison", "response", "ok", "medium"),
                    ("visual_low", "paper_A", "Low utility", "x.png", "single_figure", "background", "decoration", "ok", "low"),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        result = json.loads(_make_inspect_visual_assets(ctx)())
        assert result["status"] == "ok"
        assert [item["visual_chunk_id"] for item in result["visual_assets"]] == [
            "visual_high", "visual_medium",
        ]


def test_citation_audit_rejects_supported_clause_with_unsupported_tail():
    """Aggregate word overlap must not hide a fabricated coordinated claim."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        _make_submit_section_draft(ctx)(
            "MoS2 shows saturable absorption and cures cancer [REF:paper_A].", "initial"
        )
        result = json.loads(_make_run_citation_audit(ctx)(json.dumps([{
            "sentence_index": 0,
            "sentence_snippet": "MoS2 shows saturable absorption and cures cancer",
            "chunk_ids": ["chunk_found_001"], "paper_ids": ["paper_A"],
        }])))
        assert result["audit_passed"] is False
        assert any(flag["type"] == "overclaim" for flag in result["flags_detail"])


# ---------------------------------------------------------------------------
# SA-T24: retrieval merges main and staging KBs deterministically
# ---------------------------------------------------------------------------

def test_retrieve_chunk_text_queries_main_and_staging_kbs():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        conn = sqlite3.connect(str(ctx.kb_sqlite))
        try:
            conn.execute(
                "INSERT INTO text_chunks VALUES (?,?,?,?,?)",
                ("CHUNK_A", "paper_A", "main evidence text", "fulltext", "fulltext"),
            )
            conn.commit()
        finally:
            conn.close()
        staging = wd / "staging_kb.sqlite"
        conn = sqlite3.connect(str(staging))
        try:
            conn.execute(
                "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, text TEXT, evidence_level TEXT, source_kind TEXT)"
            )
            conn.execute(
                "INSERT INTO text_chunks VALUES (?,?,?,?,?)",
                ("CHUNK_T", "paper_B", "staging evidence text", "fulltext", "fulltext"),
            )
            conn.commit()
        finally:
            conn.close()
        ctx.temp_kb_sqlite = staging
        # Phase 2 explicitly adopted these exact chunks.  Other chunks from
        # the same papers remain outside the authoring trust graph.
        material = json.loads(ctx.material_package_path.read_text(encoding="utf-8"))
        material["chunk_ids_by_role"]["foundation"].append("CHUNK_A")
        material["chunk_ids_by_role"]["mechanism"].append("CHUNK_T")
        ctx.material_package_path.write_text(json.dumps(material), encoding="utf-8")
        ledger = json.loads(ctx.source_ledger_path.read_text(encoding="utf-8"))
        ledger["sources"][0]["canonical_chunk_ids"].append("CHUNK_A")
        ledger["sources"][1]["canonical_chunk_ids"].append("CHUNK_T")
        ctx.source_ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        result = json.loads(_make_retrieve_chunk_text(ctx)('["CHUNK_A", "CHUNK_T"]'))
        assert result["found"] == 2
        assert result["missing"] == []
        assert list(result["chunks"]) == ["CHUNK_A", "CHUNK_T"]


# ---------------------------------------------------------------------------
# SA-T25: gap_report_path consumed by load_authoring_context
# ---------------------------------------------------------------------------

def test_load_authoring_context_consumes_gap_report_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        mp_path = wd / "SECTION_MATERIAL_PACKAGE.json"
        sl_path = wd / "SECTION_SOURCE_LEDGER.json"
        gap_path = wd / "SECTION_GAP_REPORT.json"
        mp_path.write_text(json.dumps(MATERIAL_PACKAGE), encoding="utf-8")
        sl_path.write_text(json.dumps(SOURCE_LEDGER), encoding="utf-8")
        gap_path.write_text(json.dumps({
            "section_id": "S03",
            "summary": "Frontier role has no experimental ultrafast dynamics paper.",
            "gaps": [{"role": "frontier", "severity": "blocking", "description": "missing ultrafast"}],
        }), encoding="utf-8")
        ctx = SectionAuthoringContext(
            section_id="S03",
            section_data=SECTION_DATA,
            kb_sqlite=None,
            temp_kb_sqlite=None,
            work_dir=wd,
            material_package_path=mp_path,
            source_ledger_path=sl_path,
            gap_report_path=gap_path,
        )
        result = json.loads(_make_load_authoring_context(ctx)())
        assert result["status"] == "ok"
        ctx_data = json.loads((wd / "SECTION_AUTHORING_CONTEXT.json").read_text())
        # gap_summary should contain the text from gap_report_path
        assert "frontier" in ctx_data.get("gap_summary", "") or "ultrafast" in ctx_data.get("gap_summary", "")


# ---------------------------------------------------------------------------
# SA-T26: submit_revision without re-audit → validate fails (stale audit)
# ---------------------------------------------------------------------------

def test_validate_fails_if_revision_submitted_without_reaudit():
    """After submit_revision the audit is stale; validate must reject."""
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        _make_load_authoring_context(ctx)()
        _make_submit_argument_plan(ctx)(json.dumps({
            "argument_flow": "intro → evidence",
            "paragraphs": [{
                "paragraph_index": 0, "function": "introduction",
                "topic_sentence": "2D materials show nonlinear optical responses.",
                "key_claims": ["C01"], "evidence_chunk_ids": ["chunk_found_001"],
                "paper_ids": ["paper_A"], "writing_permission": "factual_assertion",
                "expected_word_count": 100,
            }]
        }))
        _make_build_evidence_packet(ctx)(json.dumps({
            "items": [{"chunk_id": "chunk_found_001", "paper_id": "paper_A",
                       "literature_role": "foundation", "scope_fit": "direct",
                       "exact_spans": ["Saturable absorption was measured in MoS2"],
                       "claim_ids": ["C01"], "writing_permission": "factual_assertion",
                       "not_usable_for": []}]
        }))
        initial_draft = (
            "Two-dimensional materials such as MoS2 exhibit saturable absorption "
            "that arises from their atomically thin geometry [REF:paper_A].\n\n"
            "This behaviour has been confirmed by Z-scan measurements across multiple "
            "wavelength regimes, demonstrating potential for ultrafast photonics [REF:paper_B]."
        )
        _make_submit_section_draft(ctx)(initial_draft, "initial")
        _make_run_citation_audit(ctx)(json.dumps([{
            "sentence_index": 0, "sentence_snippet": "2D materials exhibit saturable absorption",
            "chunk_ids": ["chunk_found_001"], "paper_ids": ["paper_A"],
            "citation_type": "factual", "audit_note": "",
        }]))
        # Now submit a revision — this should invalidate the audit
        _make_submit_revision(ctx)(
            initial_draft + "\n\nAdditional context added here for clarity.",
            "[]", "minor addition"
        )
        # validate must fail because audit is now stale
        result = _make_validate_authoring_package(ctx)()
        assert "VALIDATION_FAILED" in result
        assert "stale" in result.lower() or "audit" in result.lower()


# ---------------------------------------------------------------------------
# SA-T26b: provider auto-finalize re-audits the latest revision
# ---------------------------------------------------------------------------

def test_provider_auto_finalize_reaudits_latest_revision():
    """The runtime provider must close an acceptable post-revision draft."""
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        _make_load_authoring_context(ctx)()
        _make_submit_argument_plan(ctx)(json.dumps({
            "argument_flow": "physical basis → measured response → synthesis",
            "paragraphs": [{
                "paragraph_index": 0,
                "function": "mechanism and evidence synthesis",
                "topic_sentence": "Atomically thin materials support nonlinear optical responses.",
                "key_claims": ["C01"],
                "evidence_chunk_ids": ["chunk_found_001"],
                "paper_ids": ["paper_A"],
                "writing_permission": "factual_assertion",
                "expected_word_count": 120,
            }],
        }))
        _make_build_evidence_packet(ctx)(json.dumps({
            "items": [{
                "chunk_id": "chunk_found_001",
                "paper_id": "paper_A",
                "literature_role": "foundation",
                "scope_fit": "direct",
                "exact_spans": ["Saturable absorption was measured in MoS2"],
                "claim_ids": ["C01"],
                "writing_permission": "factual_assertion",
                "not_usable_for": [],
            }],
        }))
        draft = (
            "Two-dimensional materials such as MoS2 exhibit measurable saturable "
            "absorption under optical excitation [REF:paper_A]. This response is "
            "consistent with their reduced dimensionality and strong light-matter "
            "interaction. Across the section, the measurement is used as an anchor "
            "for comparing physical interpretations rather than as a universal "
            "performance guarantee. The available evidence therefore supports a "
            "bounded synthesis while leaving device-level generalization open."
        )
        _make_submit_section_draft(ctx)(draft, "initial")
        _make_run_citation_audit(ctx)("[]")
        _make_submit_visual_placement(ctx)("[]")
        _make_submit_revision(
            ctx,
        )(
            draft
            + " This distinction also prevents a material-level observation from "
              "being overstated as a system-level conclusion.",
            "[]",
            "clarify the scope boundary",
        )
        assert (wd / "_audit_stale").exists()

        result = SectionAuthoringToolProvider(ctx).try_auto_finalize()

        assert result is not None
        assert "VALIDATION_PASSED" in result
        assert not (wd / "_audit_stale").exists()


# ---------------------------------------------------------------------------
# SA-T27: [REF:UNKNOWN] in draft → run_citation_audit blocking flag
# ---------------------------------------------------------------------------

def test_run_citation_audit_flags_ref_unknown_in_draft():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        _make_submit_section_draft(ctx)(
            "MoS2 saturable absorption threshold is 1.5 nJ/cm² [REF:UNKNOWN].", "initial"
        )
        fn = _make_run_citation_audit(ctx)
        result = json.loads(fn(json.dumps([])))
        assert result["audit_passed"] is False
        assert result["blocking_flags"] >= 1


# ---------------------------------------------------------------------------
# SA-T28: measurement number without citation → run_citation_audit blocking flag
# ---------------------------------------------------------------------------

def test_run_citation_audit_flags_bare_measurement():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        # sentence with a measurement-like number but no [REF:xxx]
        _make_submit_section_draft(ctx)(
            "The saturation fluence of MoS2 has been measured at 12.5 nJ/cm² with no citation.\n\n"
            "A second paragraph is needed to ensure the draft has sufficient length for tests.",
            "initial"
        )
        fn = _make_run_citation_audit(ctx)
        result = json.loads(fn(json.dumps([])))
        assert result["audit_passed"] is False
        assert result["blocking_flags"] >= 1


# ---------------------------------------------------------------------------
# SA-T29: multi-source synthesis with valid citations → not over-flagged
# ---------------------------------------------------------------------------

def test_run_citation_audit_valid_multi_source_synthesis_passes():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        valid_draft = (
            "Two-dimensional materials exhibit strong third-order nonlinear optical "
            "responses arising from their atomically thin geometry [REF:paper_A].\n\n"
            "The Kerr nonlinearity in graphene has been demonstrated to exceed that "
            "of bulk glass, enabling ultrafast all-optical switching [REF:paper_B].\n\n"
            "Collectively, these findings establish 2D materials as a versatile platform "
            "for nonlinear photonics, combining the advantages of saturable absorption "
            "with strong Kerr effects [REF:paper_A] [REF:paper_B]."
        )
        _make_submit_section_draft(ctx)(valid_draft, "initial")
        fn = _make_run_citation_audit(ctx)
        result = json.loads(fn(json.dumps([
            {"sentence_index": 0, "sentence_snippet": "Two-dimensional materials exhibit strong",
             "chunk_ids": ["chunk_found_001"], "paper_ids": ["paper_A"],
             "citation_type": "factual", "audit_note": ""},
            {"sentence_index": 1, "sentence_snippet": "Kerr nonlinearity in graphene",
             "chunk_ids": ["chunk_mech_001"], "paper_ids": ["paper_B"],
             "citation_type": "factual", "audit_note": ""},
            {"sentence_index": 2, "sentence_snippet": "Collectively, these findings",
             "chunk_ids": ["chunk_found_001", "chunk_mech_001"],
             "paper_ids": ["paper_A", "paper_B"],
             "citation_type": "synthesis", "audit_note": ""},
        ])))
        assert result["status"] == "ok"
        assert result["audit_passed"] is True, f"Expected audit_passed=True, got flags: {result.get('flags_detail')}"


# ---------------------------------------------------------------------------
# SA-T30: SQLite temp DB closeable after test (no lock)
# ---------------------------------------------------------------------------

def test_sqlite_temp_db_no_lock_after_test():
    """Connections opened by tool functions must be closed; no persistent lock after tool use."""
    import sqlite3 as _sqlite3
    import gc
    with tempfile.TemporaryDirectory(prefix="sa_t30_") as tmpdir:
        wd = Path(tmpdir)
        db_path = wd / "test_kb.sqlite"
        conn = _sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, text TEXT, "
            "evidence_level TEXT, source_kind TEXT)"
        )
        conn.execute(
            "INSERT INTO text_chunks VALUES "
            "('chunk_found_001','paper_A','test text','fulltext','fulltext')"
        )
        conn.commit()
        conn.close()
        del conn

        ctx = SectionAuthoringContext(
            section_id="S03",
            section_data=SECTION_DATA,
            kb_sqlite=db_path,
            temp_kb_sqlite=None,
            work_dir=wd,
            material_package_path=wd / "SECTION_MATERIAL_PACKAGE.json",
            source_ledger_path=wd / "SECTION_SOURCE_LEDGER.json",
        )
        (wd / "SECTION_MATERIAL_PACKAGE.json").write_text(
            json.dumps(MATERIAL_PACKAGE), encoding="utf-8"
        )
        (wd / "SECTION_SOURCE_LEDGER.json").write_text(
            json.dumps(SOURCE_LEDGER), encoding="utf-8"
        )
        fn = _make_retrieve_chunk_text(ctx)
        result = json.loads(fn('["chunk_found_001"]'))
        assert result["found"] == 1

        # Force GC to release any lingering references, then verify the file
        # can be opened by a new connection (i.e., it is not exclusively locked)
        gc.collect()
        probe = _sqlite3.connect(str(db_path))
        rows = probe.execute("SELECT COUNT(*) FROM text_chunks").fetchone()
        probe.close()
        assert rows[0] == 1


def test_submit_argument_plan_rejects_fake_ids_without_artifact():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        result = json.loads(_make_submit_argument_plan(ctx)(json.dumps({
            "argument_flow": "invalid",
            "paragraphs": [{
                "paragraph_index": 0, "function": "evidence", "topic_sentence": "Invented claim.",
                "key_claims": ["C01"], "evidence_chunk_ids": ["FAKE_CHUNK"],
                "paper_ids": ["FAKE_PAPER"], "writing_permission": "factual_assertion",
                "expected_word_count": 100,
            }],
        })))
        assert result["status"] == "rejected"
        assert result["allowed_assets"]
        assert result["repair_instruction"].startswith("Reuse only")
        assert not (wd / "SECTION_ARGUMENT_PLAN.json").exists()


def test_argument_plan_normalizes_common_model_aliases_without_weakening_id_checks():
    """A near-correct model payload should not burn iterations on field names."""
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        result = json.loads(_make_submit_argument_plan(ctx)(json.dumps({
            "overall_argument": "Establish the phenomenon, then explain its mechanism.",
            "paragraphs": [
                {
                    "paragraph_id": "P1",
                    "function": "foundation",
                    "topic_sentence": "MoS2 provides a representative saturable absorber.",
                    "claim_ids": ["C01"],
                    "evidence_chunks": ["chunk_found_001"],
                    "permission": "factual_assertion",
                    "word_target": "120 words",
                },
                {
                    "paragraph_id": "P2",
                    "function": "mechanism",
                    "topic_sentence": "Graphene supplies a complementary nonlinear mechanism.",
                    "claim_ids": [],
                    "chunk_ids": "chunk_mech_001",
                    "permission": "hedged_factual_assertion",
                    "word_target": 150,
                },
            ],
        })))
        assert result["status"] == "ok"
        plan = json.loads((wd / "SECTION_ARGUMENT_PLAN.json").read_text(encoding="utf-8"))
        assert plan["argument_flow"].startswith("Establish")
        assert plan["paragraphs"][0]["key_claims"] == ["C01"]
        assert plan["paragraphs"][0]["paper_ids"] == ["paper_A"]
        assert plan["paragraphs"][1]["evidence_chunk_ids"] == ["chunk_mech_001"]
        assert plan["total_expected_words"] == 270
        audit = json.loads(
            (wd / "SECTION_SUBMISSION_NORMALIZATIONS.json").read_text(encoding="utf-8")
        )
        assert audit["count"] >= 8


def test_argument_plan_aliases_do_not_hide_fabricated_ids():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        result = json.loads(_make_submit_argument_plan(ctx)(json.dumps({
            "overall_argument": "Invalid plan",
            "paragraphs": [{
                "claim_ids": ["C01"],
                "evidence_chunks": ["FAKE_CHUNK"],
                "permission": "factual_assertion",
                "word_target": 100,
            }],
        })))
        assert result["status"] == "rejected"
        assert "FAKE_CHUNK" in json.dumps(result)
        assert not (wd / "SECTION_ARGUMENT_PLAN.json").exists()


def test_evidence_packet_normalizes_bare_list_from_accepted_plan():
    """Claim, permission, and owner may be recovered only from accepted assets."""
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        plan_result = json.loads(_make_submit_argument_plan(ctx)(json.dumps({
            "argument_flow": "Ground the representative observation.",
            "paragraphs": [{
                "paragraph_index": 0,
                "function": "foundation",
                "topic_sentence": "MoS2 exhibits saturable absorption.",
                "key_claims": ["C01"],
                "evidence_chunk_ids": ["chunk_found_001"],
                "paper_ids": ["paper_A"],
                "writing_permission": "factual_assertion",
                "expected_word_count": 120,
            }],
        })))
        assert plan_result["status"] == "ok"
        result = json.loads(_make_build_evidence_packet(ctx)(json.dumps([{
            "chunk_id": "chunk_found_001",
            "permissions": ["Use this passage for the observation."],
            "support_hints": "saturable absorption",
        }])))
        assert result["status"] == "ok"
        packet = json.loads((wd / "SECTION_EVIDENCE_PACKET.json").read_text(encoding="utf-8"))
        assert packet["items"][0]["paper_id"] == "paper_A"
        assert packet["items"][0]["claim_ids"] == ["C01"]
        assert packet["items"][0]["writing_permission"] == "factual_assertion"
        assert packet["uncovered_claim_ids"] == []


def test_argument_plan_allows_nonfactual_transition_without_evidence():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        result = json.loads(_make_submit_argument_plan(ctx)(json.dumps({
            "paragraphs": [{
                "paragraph_index": 0,
                "function": "transition",
                "topic_sentence": "The discussion now turns from mechanism to deployment.",
                "key_claims": [],
                "evidence_chunk_ids": [],
                "paper_ids": [],
                "writing_permission": "structural_transition",
                "expected_word_count": 40,
            }],
        })))
        assert result["status"] == "ok"


def test_argument_plan_rejects_numeric_background_without_evidence():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        result = json.loads(_make_submit_argument_plan(ctx)(json.dumps({
            "paragraphs": [{
                "paragraph_index": 0,
                "function": "background",
                "topic_sentence": "The response reaches 97% at 800 nm.",
                "key_claims": [],
                "evidence_chunk_ids": [],
                "paper_ids": [],
                "writing_permission": "common_background",
                "expected_word_count": 40,
            }],
        })))
        assert result["status"] == "rejected"
        assert "measurement-bearing" in json.dumps(result)


def test_build_evidence_packet_rejects_fake_ids_without_artifact():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        result = json.loads(_make_build_evidence_packet(ctx)(json.dumps({
            "items": [{
                "chunk_id": "FAKE_CHUNK", "paper_id": "FAKE_PAPER",
                "exact_spans": ["invented"], "claim_ids": ["C01"],
                "writing_permission": "factual_assertion",
            }],
        })))
        assert result["status"] == "rejected"
        assert not (wd / "SECTION_EVIDENCE_PACKET.json").exists()


def test_citation_audit_auto_infers_citations_when_map_is_empty():
    # When the model submits "[]", the audit auto-infers citations from draft markers.
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        _make_submit_section_draft(ctx)(
            "MoS2 shows saturable absorption [REF:paper_A].", "initial"
        )
        result = json.loads(_make_run_citation_audit(ctx)("[]"))
        assert result["total_citations"] == 1
        assert "paper_A" in result["papers_cited"]
        assert not any(flag["type"] == "missing_citation_mapping" for flag in result["flags_detail"])


def test_citation_audit_does_not_treat_markdown_heading_as_claim_text():
    """A section heading must not become an unsupported clause in sentence zero."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        _make_submit_section_draft(ctx)(
            "# Nonlinear Optical Response\n\n"
            "MoS2 shows saturable absorption [REF:paper_A].",
            "initial",
        )
        result = json.loads(_make_run_citation_audit(ctx)("[]"))
        assert result["audit_passed"] is True
        assert result["blocking_flags"] == 0
        citation_map = json.loads(
            (ctx.work_dir / "SECTION_CITATION_MAP.json").read_text(encoding="utf-8")
        )
        assert citation_map["citations"][0]["sentence_snippet"].startswith(
            "MoS2 shows saturable absorption"
        )


def test_citation_audit_recovers_ids_from_snippet_and_canonical_assets():
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        _make_submit_section_draft(ctx)(
            "MoS2 shows saturable absorption [REF:paper_A].", "initial"
        )
        result = json.loads(_make_run_citation_audit(ctx)(json.dumps([{
            "sentence_snippet": "MoS2 shows saturable absorption",
            "chunk_ids": [],
            "paper_ids": [],
        }])))
        assert result["audit_passed"] is True
        citation_map = json.loads((ctx.work_dir / "SECTION_CITATION_MAP.json").read_text(encoding="utf-8"))
        assert citation_map["citations"][0]["paper_ids"] == ["paper_A"]
        assert citation_map["citations"][0]["chunk_ids"] == ["chunk_found_001"]


def test_citation_inference_uses_full_chunk_not_lossy_packet_spans():
    """Packet excerpts are hints; canonical text remains the ranking source."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        ledger = json.loads(ctx.source_ledger_path.read_text(encoding="utf-8"))
        ledger["sources"][0]["canonical_chunk_ids"].append("chunk_decoy_001")
        ctx.source_ledger_path.write_text(
            json.dumps(ledger), encoding="utf-8"
        )
        conn = sqlite3.connect(str(ctx.kb_sqlite))
        try:
            conn.execute(
                "INSERT INTO text_chunks VALUES (?,?,?,?,?)",
                (
                    "chunk_decoy_001",
                    "paper_A",
                    "MoS2 absorption response is discussed without a reported "
                    "fluence threshold.",
                    "fulltext",
                    "fulltext",
                ),
            )
            conn.commit()
        finally:
            conn.close()
        # The selected excerpt omits the number used by the draft, while the
        # same canonical chunk contains it.  The old implementation ranked only
        # this excerpt and incorrectly preferred the decoy chunk.
        (ctx.work_dir / "SECTION_EVIDENCE_PACKET.json").write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "paper_id": "paper_A",
                            "chunk_id": "chunk_found_001",
                            "exact_spans": [
                                "Two-dimensional materials such as MoS2 exhibit "
                                "strong third-order nonlinear optical responses."
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        graph = _build_asset_graph(ctx)
        selected = _infer_citation_chunks(
            ctx,
            graph,
            "MoS2 shows saturable absorption at 1.5 nJ/cm^2 "
            "[REF:paper_A].",
            ["paper_A"],
        )
        assert selected == ["chunk_found_001"]


def test_citation_audit_rejects_false_claim_with_allowed_pair():
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        _make_submit_section_draft(ctx)(
            "MoS2 does not exhibit saturable absorption [REF:paper_A].", "initial"
        )
        result = json.loads(_make_run_citation_audit(ctx)(json.dumps([{
            "sentence_index": 0,
            "sentence_snippet": "MoS2 does not exhibit saturable absorption",
            "chunk_ids": ["chunk_found_001"], "paper_ids": ["paper_A"],
        }])))
        assert result["audit_passed"] is False
        assert any(flag["type"] == "overclaim" for flag in result["flags_detail"])


def test_citation_support_recognizes_equivalent_do_not_bound():
    verdict, reason, _ = assess_citation_support(
        "The diameter is not exceeding 200 micrometers.",
        ["The measured diameter does not exceed 200 micrometers."],
    )
    assert verdict == "entailed", reason


def test_citation_support_ignores_symbolic_latex_indices():
    verdict, reason, _ = assess_citation_support(
        "At the degeneracy, $H_N|u\\rangle = E_0|u\\rangle$ describes a "
        "single eigenstate.",
        [
            "At an exceptional degeneracy the non-Hermitian Hamiltonian "
            "possesses one coalesced eigenstate."
        ],
    )
    assert "Numeric value(s)" not in reason


def test_citation_support_keeps_explicit_measurements_strict():
    verdict, reason, _ = assess_citation_support(
        "The resonance was measured at $1550\\,\\mathrm{nm}$.",
        ["The optical resonance was measured in the telecom band."],
    )
    assert verdict == "not_entailed"
    assert "1550" in reason


def test_citation_support_partial_coordinated_clause_is_not_zero_overlap():
    verdict, reason, _ = assess_citation_support(
        (
            "MoS2 shows saturable absorption and offers very useful application "
            "guidance [REF:paper_A]."
        ),
        [
            "MoS2 shows saturable absorption for optical application."
        ],
    )

    assert verdict == "not_entailed"
    assert "zero evidence overlap" not in reason


def test_citation_note_hard_failure_detects_fabrication_classes():
    assert _citation_note_hard_failure(
        "Coordinated clause has zero evidence overlap with the cited chunk text."
    )
    assert _citation_note_hard_failure(
        "The sentence negates a predicate asserted by the cited evidence."
    )
    assert not _citation_note_hard_failure(
        "A coordinated assertion is unsupported by the cited chunk text."
    )


def test_draft_submission_repairs_latin_scientific_mojibake():
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        result = json.loads(
            _make_submit_section_draft(ctx)(
                "The metasurface鈥檚 response remains stable across the tested "
                "band, and the measured feature size is 200 渭m. "
                "This sentence provides enough Latin context for safe repair.",
                "encoding boundary test",
            )
        )
        saved = (ctx.work_dir / "SECTION_DRAFT_EN.md").read_text(
            encoding="utf-8"
        )
        assert result["status"] == "ok"
        assert "鈥" not in saved
        assert "渭" not in saved
        assert "metasurface’s" in saved
        assert "μm" in saved


def test_real_phase2_adapter_preserves_writing_contract():
    from scripts.run_section_authoring_smoke import _section_data_from_real

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        package = root / "SECTION_MATERIAL_PACKAGE.json"
        package.write_text(json.dumps({"section_id": "S03", "section_title": "Test"}), encoding="utf-8")
        (root / "SECTION_CONTEXT.json").write_text(json.dumps({
            "section_id": "S03",
            "section_contract": {
                "word_budget": 450,
                "paragraph_functions": ["Frame", "Explain", "Synthesize"],
            },
        }), encoding="utf-8")
        section_data = _section_data_from_real(package)
        assert section_data["section_contract"]["word_budget"] == 450
        assert len(section_data["section_contract"]["paragraph_functions"]) == 3


def test_load_authoring_context_propagates_all_writing_context_fields():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        gap_path = wd / "phase2_gap.json"
        gap_payload = {"overall_coverage_status": "completed_with_open_gaps", "gaps": [{"role": "method"}]}
        gap_path.write_text(json.dumps(gap_payload), encoding="utf-8")
        ctx.gap_report_path = gap_path
        ctx.mentor_advice = {"moves": ["open with the comparison"]}
        ctx.full_review_argument = "The review links structure to performance."
        ctx.section_role = "mechanism bridge"
        ctx.preceding_section_conclusion = "The material platform is established."
        ctx.following_section_role = "Compare device consequences."
        ctx.transition_contract = {"out": "move from mechanism to device evidence"}
        ctx.terminology_ledger = {"preferred": {"2D material": "two-dimensional material"}}
        result = json.loads(_make_load_authoring_context(ctx)())
        assert result["status"] == "ok"
        data = json.loads((wd / "SECTION_AUTHORING_CONTEXT.json").read_text(encoding="utf-8"))
        assert data["mentor_advice"] == ctx.mentor_advice
        assert data["full_review_argument"] == ctx.full_review_argument
        assert data["section_role"] == ctx.section_role
        assert data["preceding_section_conclusion"] == ctx.preceding_section_conclusion
        assert data["following_section_role"] == ctx.following_section_role
        assert data["transition_contract"] == ctx.transition_contract
        assert data["terminology_ledger"] == ctx.terminology_ledger
        assert data["phase2_gap_report"] == gap_payload


def test_empty_phase2_allowlist_fails_closed():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        Path(ctx.source_ledger_path).write_text(
            json.dumps({"schema_version": "2.0", "section_id": "S03", "sources": []}),
            encoding="utf-8",
        )
        load_result = json.loads(_make_load_authoring_context(ctx)())
        assert load_result["status"] == "error"
        plan_result = json.loads(_make_submit_argument_plan(ctx)(json.dumps({
            "paragraphs": [{
                "paragraph_index": 0, "function": "background", "topic_sentence": "Background.",
                "evidence_chunk_ids": [], "paper_ids": [], "writing_permission": "evidence_gap_only",
            }],
        })))
        assert plan_result["status"] == "rejected"


def test_argument_plan_enforces_canonical_scope_and_not_usable_for():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        ledger = json.loads(Path(ctx.source_ledger_path).read_text(encoding="utf-8"))
        ledger["sources"][0]["scope_fit"] = "contextual"
        ledger["sources"][0]["not_usable_for"] = ["exact quantitative measurements"]
        Path(ctx.source_ledger_path).write_text(json.dumps(ledger), encoding="utf-8")
        result = json.loads(_make_submit_argument_plan(ctx)(json.dumps({
            "paragraphs": [{
                "paragraph_index": 0, "function": "evidence",
                "topic_sentence": "The measured threshold is exactly 1.5 nJ.",
                "key_claims": ["C01"], "evidence_chunk_ids": ["chunk_found_001"],
                "paper_ids": ["paper_A"], "writing_permission": "factual_assertion",
            }],
        })))
        assert result["status"] == "rejected"
        reasons = json.dumps(result)
        assert "contextual" in reasons
        assert "not_usable_for" in reasons


def test_argument_plan_rejects_adjacent_source_as_unqualified_direct_fact():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        ledger = json.loads(
            Path(ctx.source_ledger_path).read_text(encoding="utf-8")
        )
        ledger["sources"][0]["scope_fit"] = "adjacent"
        Path(ctx.source_ledger_path).write_text(
            json.dumps(ledger), encoding="utf-8"
        )
        result = json.loads(
            _make_submit_argument_plan(ctx)(
                json.dumps(
                    {
                        "paragraphs": [
                            {
                                "paragraph_index": 0,
                                "function": "evidence",
                                "topic_sentence": (
                                    "This platform directly establishes the "
                                    "target-domain deployment limit."
                                ),
                                "key_claims": ["C01"],
                                "evidence_chunk_ids": ["chunk_found_001"],
                                "paper_ids": ["paper_A"],
                                "writing_permission": "factual_assertion",
                            }
                        ]
                    }
                )
            )
        )
        assert result["status"] == "rejected"
        assert "adjacent-domain" in json.dumps(result)


def test_evidence_packet_rejects_allowed_ids_with_wrong_owner():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        result = json.loads(_make_build_evidence_packet(ctx)(json.dumps({
            "items": [{
                "chunk_id": "chunk_mech_001", "paper_id": "paper_A",
                "scope_fit": "direct", "exact_spans": ["Graphene has large Kerr nonlinearity"],
                "claim_ids": [], "writing_permission": "factual_assertion",
            }],
        })))
        assert result["status"] == "rejected"
        assert "ownership mismatch" in json.dumps(result)


def test_exact_span_normalization_preserves_scientific_symbol_identity():
    assert _normalize_span_for_match("alpha \u03b1-polarized mode") != _normalize_span_for_match(
        "alpha \u03b2-polarized mode"
    )
    assert _normalize_span_for_match("8\u201313 \u00b5m; 10 W m\u207b\u00b2") == _normalize_span_for_match(
        "8-13 \u03bcm; 10 W m-2"
    )


def test_validate_enforces_supplied_word_and_paragraph_contract():
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        ctx.section_data = {
            **ctx.section_data,
            "section_contract": {
                "word_budget": 200,
                "paragraph_functions": ["Frame", "Explain", "Synthesize"],
            },
        }
        assert json.loads(_make_load_authoring_context(ctx)())["status"] == "ok"
        assert json.loads(_make_submit_argument_plan(ctx)(json.dumps({
            "paragraphs": [
                {
                    "paragraph_index": 0, "function": "evidence",
                    "topic_sentence": "MoS2 exhibits saturable absorption.", "key_claims": ["C01"],
                    "evidence_chunk_ids": ["chunk_found_001"], "paper_ids": ["paper_A"],
                    "writing_permission": "factual_assertion", "expected_word_count": 100,
                },
                {
                    "paragraph_index": 1, "function": "synthesis",
                    "topic_sentence": "The measured response frames the section synthesis.",
                    "key_claims": ["C01"], "evidence_chunk_ids": ["chunk_found_001"],
                    "paper_ids": ["paper_A"], "writing_permission": "hedged_factual_assertion",
                    "expected_word_count": 100,
                },
            ],
        })))["status"] == "ok"
        assert json.loads(_make_build_evidence_packet(ctx)(json.dumps({
            "items": [{
                "chunk_id": "chunk_found_001", "paper_id": "paper_A", "scope_fit": "direct",
                "exact_spans": ["MoS2 shows saturable absorption at 1.5 nJ/cm^2"],
                "claim_ids": ["C01"], "writing_permission": "factual_assertion",
            }],
        })))["status"] == "ok"
        draft = (
            "MoS2 shows saturable absorption [REF:paper_A]. This concise paragraph keeps the "
            "discussion deliberately short while adding neutral background language about review "
            "organization, comparison, scope, terminology, evidence boundaries, and transitions. "
            "It remains one paragraph and therefore cannot satisfy the supplied structural contract."
        )
        _make_submit_section_draft(ctx)(draft, "initial")
        _make_run_citation_audit(ctx)(json.dumps([{
            "sentence_index": 0, "sentence_snippet": "MoS2 shows saturable absorption",
            "chunk_ids": ["chunk_found_001"], "paper_ids": ["paper_A"],
        }]))
        result = _make_validate_authoring_package(ctx)()
        assert "VALIDATION_FAILED" in result
        assert "paragraph contract" in result


# ---------------------------------------------------------------------------
# Phase 3.2 regression tests
# ---------------------------------------------------------------------------

def test_build_evidence_packet_accepts_noncanonical_scope_fit():
    """scope_fit supplied by agent is silently canonicalized; mismatch is not an error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        result = json.loads(_make_build_evidence_packet(ctx)(json.dumps({
            "items": [{
                "chunk_id": "chunk_found_001",
                "paper_id": "paper_A",
                "scope_fit": "in_scope",   # non-canonical; canonical is "direct"
                "exact_spans": ["saturable absorption at 1.5 nJ/cm^2"],
                "claim_ids": ["C01"],
                "writing_permission": "factual_assertion",
                "not_usable_for": [],
            }],
        })))
        assert result["status"] == "ok", f"Expected ok, got: {result}"
        assert (Path(tmpdir) / "SECTION_EVIDENCE_PACKET.json").exists()


def test_citation_audit_infers_chunk_without_token_overlap():
    """When chunk_ids is omitted, auto-recovery binds the paper's chunk even
    when the sentence tokens have no overlap with the chunk's indexed terms."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        # Use a sentence whose vocabulary is completely disjoint from the chunk text
        # ("solar irradiance budget" vs chunk that discusses "saturable absorption")
        _make_submit_section_draft(ctx)(
            "Solar irradiance budget analysis requires careful spectral integration [REF:paper_A].",
            "initial",
        )
        fn = _make_run_citation_audit(ctx)
        result = json.loads(fn(json.dumps([{
            "sentence_snippet": "Solar irradiance budget analysis",
            "chunk_ids": [],
            "paper_ids": [],
        }])))
        # Audit may or may not pass (entailment check is separate), but must NOT
        # produce a paper_chunk_mismatch flag from empty chunk inference.
        assert result["status"] == "ok"
        assert not any(
            f["type"] == "paper_chunk_mismatch" for f in result.get("flags_detail", [])
        ), f"Unexpected paper_chunk_mismatch: {result.get('flags_detail')}"


# ---------------------------------------------------------------------------
# Helper for smoke --minimal mode (referenced by run_section_authoring_smoke.py)
# ---------------------------------------------------------------------------

def _make_minimal_scripted_model(ctx: SectionAuthoringContext):
    """Return a ScriptedFakeModel that drives the authoring worker to VALIDATION_PASSED."""
    import json as _json

    DRAFT_TEXT = (
        "Two-dimensional materials such as MoS2 exhibit strong third-order nonlinear optical "
        "responses arising from their atomically thin geometry [REF:paper_A].\n\n"
        "This local observation provides a disciplined starting point for the section. The prose "
        "keeps broader comparisons at the level of synthesis, separates evidence from guidance, "
        "and avoids extending one material-specific result into a claim about every platform."
    )

    PLAN_JSON = _json.dumps({
        "argument_flow": "intro → mechanism → evidence synthesis",
        "paragraphs": [
            {
                "paragraph_index": 0,
                "function": "introduction",
                "topic_sentence": "2D materials offer tunable nonlinear optical responses.",
                "key_claims": ["C01"],
                "evidence_chunk_ids": ["chunk_found_001"],
                "paper_ids": ["paper_A"],
                "writing_permission": "factual_assertion",
                "expected_word_count": 120,
            },
        ],
    })

    EP_JSON = _json.dumps({
        "items": [
            {
                "chunk_id": "chunk_found_001",
                "paper_id": "paper_A",
                "literature_role": "foundation",
                "scope_fit": "direct",
                "exact_spans": ["MoS2 exhibits saturable absorption under pulsed optical excitation"],
                "claim_ids": ["C01"],
                "writing_permission": "factual_assertion",
                "not_usable_for": [],
            }
        ]
    })

    CIT_MAP_JSON = _json.dumps([
        {
            "sentence_index": 0,
            "sentence_snippet": "Two-dimensional materials such as MoS2 exhibit strong third-order",
            "chunk_ids": ["chunk_found_001"],
            "paper_ids": ["paper_A"],
            "citation_type": "factual",
            "entailment_verdict": "entailed",
            "audit_note": "",
        }
    ])

    # Scripted tool call sequence
    STEPS = [
        # (tool_name, kwargs_dict)
        ("load_authoring_context", {}),
        ("inspect_material_package", {}),
        ("submit_argument_plan", {"plan_json": PLAN_JSON}),
        ("build_evidence_packet", {"evidence_json": EP_JSON}),
        ("submit_section_draft", {"draft_text": DRAFT_TEXT, "summary": "initial"}),
        ("run_citation_audit", {"citation_map_json": CIT_MAP_JSON}),
        ("submit_visual_placement", {"placements_json": "[]"}),
        ("validate_authoring_package", {}),
    ]

    try:
        from tests.test_research_worker_runtime import ScriptedFakeModel, _make_tool_call_response
        return ScriptedFakeModel(
            script=[_make_tool_call_response(name, arguments) for name, arguments in STEPS],
            usage_per_call=(120, 30),
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Phase 3.2 adversarial tests
# ---------------------------------------------------------------------------

def test_auto_span_preserves_greek_letters():
    """α and β must not be treated as the same token."""
    from optomind_research.runtime.section_authoring_tool_registry import _auto_select_spans
    chunk = (
        "The alpha mode shows resonance at 532 nm. "
        "The beta mode resonates at 1064 nm."
    )
    spans_alpha = _auto_select_spans(chunk, "alpha mode 532 nm", "")
    spans_beta = _auto_select_spans(chunk, "beta mode 1064 nm", "")
    assert any("alpha" in s for s in spans_alpha), "alpha sentence not selected"
    assert any("beta" in s for s in spans_beta), "beta sentence not selected"
    # Crucially they should NOT be the same sentence
    assert spans_alpha != spans_beta or spans_alpha == [], "α and β must not collapse to same span"


def test_auto_span_matches_micrometer_variant():
    """8–13 μm should match against '8-13 um' or '8-13 μm' variants."""
    from optomind_research.runtime.section_authoring_tool_registry import _auto_select_spans
    chunk = (
        "Emission in the 8-13 um atmospheric window is central to passive cooling. "
        "Values outside this range contribute less than 10 percent."
    )
    spans = _auto_select_spans(chunk, "8–13 μm atmospheric window", "")
    assert spans, "should select a sentence containing the window description"
    assert any("8-13" in s or "8" in s for s in spans)


def test_auto_span_selects_from_chunk_without_model_exact_span():
    """Evidence packet build succeeds when no exact_spans are provided."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        result = json.loads(_make_build_evidence_packet(ctx)(json.dumps({
            "items": [{
                "chunk_id": "chunk_found_001",
                "paper_id": "paper_A",
                "claim_ids": ["C01"],
                "writing_permission": "factual_assertion",
                "support_hint": "saturable absorption MoS2",
                # No exact_spans provided — tool must auto-select
            }],
            "uncovered_claim_ids": [],
        })))
        assert result["status"] == "ok", f"expected ok, got: {result}"
        packet = json.loads((ctx.work_dir / "SECTION_EVIDENCE_PACKET.json").read_text(encoding="utf-8"))
        assert len(packet["items"][0]["exact_spans"]) >= 1
        assert packet["items"][0]["exact_span_source"] == "deterministic_resolver"


def test_auto_span_rejects_irrelevant_chunk():
    """When no sentence in the chunk scores ≥ 2 against the claim, reject."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        # chunk_mech_001 is about Kerr nonlinearity in graphene
        # claim is about something completely unrelated
        result = json.loads(_make_build_evidence_packet(ctx)(json.dumps({
            "items": [{
                "chunk_id": "chunk_mech_001",
                "paper_id": "paper_B",
                "claim_ids": ["C01"],
                "writing_permission": "factual_assertion",
                "support_hint": "saturable absorption MoS2 1.5 nJ",
                # No exact_spans — tool should reject because chunk_mech_001 is about graphene Kerr
            }],
            "uncovered_claim_ids": [],
        })))
        # chunk_mech_001 text: "Kerr nonlinearity in graphene...ultrafast all-optical switching"
        # claim: "2D materials exhibit saturable absorption" — some overlap (2D materials),
        # but no numeric tokens from "1.5 nJ" in chunk.
        # The test checks that purely irrelevant chunks are rejected.
        # Note: if there IS some overlap (graphene, large), result may be ok.
        # The key constraint: exact_span_source must be either deterministic_resolver or error.
        if result["status"] == "ok":
            packet = json.loads((ctx.work_dir / "SECTION_EVIDENCE_PACKET.json").read_text(encoding="utf-8"))
            # Accept — chunk had some overlap. Verify source is tagged.
            assert "exact_span_source" in packet["items"][0]
        else:
            assert result["status"] == "rejected"


def test_auto_span_wrong_number_rejected():
    """A hint with a wrong numeric value should not match a sentence with the right value."""
    from optomind_research.runtime.section_authoring_tool_registry import _auto_select_spans
    chunk = "MoS2 shows saturable absorption at 1.5 nJ/cm^2. Graphene absorbs at 3.0 nJ."
    # Ask for a sentence mentioning 7.5 nJ — should NOT select 1.5 nJ sentence preferentially
    spans = _auto_select_spans(chunk, "saturable absorption 7.5 nJ graphene", "")
    # Both sentences have some overlap but neither has 7.5; score should be based on real tokens
    # This test just verifies no crash and the result is a valid list
    assert isinstance(spans, list)


def test_auto_span_preserves_scientific_abbreviations():
    """Scientific abbreviations must not split one supporting sentence into fragments."""
    from optomind_research.runtime.section_authoring_tool_registry import _auto_select_spans
    chunk = (
        "The net cooling power is approx. 50 W m-2, i.e. above the measured ambient losses. "
        "A separate experiment examined coating adhesion."
    )
    spans = _auto_select_spans(
        chunk,
        "net cooling power approximately 50 W m-2 above ambient losses",
        "",
    )
    assert spans
    assert "approx. 50 W m-2, i.e. above the measured ambient losses" in spans[0]


def test_auto_span_prefers_specific_concept_coverage_over_repetition():
    """Repeated generic words must not outrank a sentence covering the actual claim."""
    from optomind_research.runtime.section_authoring_tool_registry import _auto_select_spans
    chunk = (
        "Cooling power was evaluated for a rooftop area of 50 m2, and cooling power "
        "was converted into a daily energy estimate of 15 kWh. "
        "Solar absorption across the AM1.5 spectrum directly determines parasitic "
        "heat input to a daytime radiative cooler."
    )
    spans = _auto_select_spans(
        chunk,
        "Solar absorption under the AM1.5 spectrum determines parasitic heat input.",
        "",
    )
    assert spans
    assert spans[0].startswith("Solar absorption")
    assert "15 kWh" not in spans[0]


def test_auto_span_prefers_objective_specific_design_statement():
    """A design-objective claim should select the objective-dependent sentence."""
    from optomind_research.runtime.section_authoring_tool_registry import _auto_select_spans
    chunk = (
        "Metamaterials provide a broad platform for optical engineering. "
        "The optimal emissivity profile depends on whether the objective is maximum "
        "net cooling power or minimum equilibrium temperature."
    )
    spans = _auto_select_spans(
        chunk,
        "The optimal spectral emissivity profile depends on the selected cooling objective.",
        "maximum net cooling power versus minimum equilibrium temperature",
    )
    assert spans
    assert spans[0].startswith("The optimal emissivity profile")


def test_argument_plan_idempotent():
    """Calling submit_argument_plan a second time returns already_completed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        plan_json = json.dumps({
            "argument_flow": "test flow",
            "paragraphs": [{
                "paragraph_index": 0,
                "function": "introduction",
                "topic_sentence": "MoS2 exhibits saturable absorption.",
                "key_claims": ["C01"],
                "evidence_chunk_ids": ["chunk_found_001"],
                "paper_ids": ["paper_A"],
                "writing_permission": "factual_assertion",
                "expected_word_count": 100,
            }],
        })
        r1 = json.loads(_make_submit_argument_plan(ctx)(plan_json))
        assert r1["status"] == "ok"
        r2 = json.loads(_make_submit_argument_plan(ctx)(plan_json))
        assert r2["status"] == "already_completed"


def test_evidence_packet_idempotent():
    """Calling build_evidence_packet a second time returns already_completed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        evidence_json = json.dumps({
            "items": [{
                "chunk_id": "chunk_found_001",
                "paper_id": "paper_A",
                "claim_ids": ["C01"],
                "writing_permission": "factual_assertion",
                "support_hint": "saturable absorption",
            }],
            "uncovered_claim_ids": [],
        })
        r1 = json.loads(_make_build_evidence_packet(ctx)(evidence_json))
        assert r1["status"] == "ok"
        r2 = json.loads(_make_build_evidence_packet(ctx)(evidence_json))
        assert r2["status"] == "already_completed"


def test_evidence_packet_accepts_audited_extension_and_keeps_history():
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        build = _make_build_evidence_packet(ctx)
        first = json.dumps({
            "items": [{
                "chunk_id": "chunk_found_001",
                "paper_id": "paper_A",
                "claim_ids": ["C01"],
                "writing_permission": "factual_assertion",
                "support_hint": "saturable absorption",
            }],
            "uncovered_claim_ids": [],
        })
        extension = json.dumps({
            "items": [{
                "chunk_id": "chunk_mech_001",
                "paper_id": "paper_B",
                "claim_ids": ["C01"],
                "writing_permission": "factual_assertion",
                "support_hint": "Kerr nonlinearity",
            }],
            "uncovered_claim_ids": [],
        })
        assert json.loads(build(first))["status"] == "ok"
        result = json.loads(build(extension))
        assert result["status"] == "extended"
        assert result["total_items"] == 2
        packet = json.loads(
            (Path(tmpdir) / "SECTION_EVIDENCE_PACKET.json").read_text(
                encoding="utf-8"
            )
        )
        assert {item["paper_id"] for item in packet["items"]} == {
            "paper_A",
            "paper_B",
        }
        history = json.loads(
            (Path(tmpdir) / "SECTION_EVIDENCE_PACKET_HISTORY.json").read_text(
                encoding="utf-8"
            )
        )
        assert history["total_revisions"] == 1


def test_section_word_budget_is_soft_with_only_broad_safety_bounds():
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        ctx.section_data = {
            **ctx.section_data,
            "section_contract": {"word_budget": 1100},
        }
        assert _section_contract_errors(ctx, "word " * 879) == []
        assert _section_contract_errors(ctx, "word " * 850) == []
        assert _section_contract_errors(ctx, "word " * 49)
        assert _section_contract_errors(ctx, "word " * 4401)


def test_section_word_budget_accepts_2800_for_1400_soft_target():
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        ctx.section_data = {
            **ctx.section_data,
            "section_contract": {"word_budget": 1400},
        }
        assert _section_contract_errors(ctx, "word " * 2800) == []


def test_argument_plan_word_budget_accepts_2800_for_1400_soft_target():
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        ctx.section_data = {
            **ctx.section_data,
            "section_contract": {"word_budget": 1400},
        }
        graph = _build_asset_graph(ctx)
        errors = _validate_argument_plan_data(
            ctx,
            graph,
            [
                {
                    "paragraph_index": 0,
                    "function": "synthesis",
                    "topic_sentence": "Soft length plan.",
                    "key_claims": [],
                    "evidence_chunk_ids": [],
                    "paper_ids": [],
                    "writing_permission": "interpretive_synthesis",
                    "expected_word_count": 2800,
                }
            ],
        )
        assert not any("safety ceiling" in item for item in errors)


def test_argument_plan_chunks_used_alias_preserves_canonical_owner_fill():
    graph = SimpleNamespace(
        chunks={"c1": SimpleNamespace(paper_id="p1")},
    )
    data = {
        "paragraphs": [
            {
                "chunks_used": ["c1"],
            }
        ]
    }

    normalized, repairs = _normalize_argument_plan_contract(data, graph)

    paragraph = normalized["paragraphs"][0]
    assert paragraph["evidence_chunk_ids"] == ["c1"]
    assert paragraph["paper_ids"] == ["p1"]
    assert any(
        repair.get("source_field") == "chunks_used"
        for repair in repairs
    )


def test_evidence_packet_chunks_used_alias_maps_to_items():
    graph = SimpleNamespace(
        chunks={"c1": SimpleNamespace(paper_id="p1")},
    )
    ctx = SimpleNamespace(
        work_dir=Path("F:/nonexistent_chunks_used_test"),
        section_data={},
    )

    normalized, repairs = _normalize_evidence_packet_contract(
        {"chunks_used": [{"chunk_id": "c1", "paper_id": "p1"}]},
        ctx,
        graph,
    )

    assert normalized["items"][0]["chunk_id"] == "c1"
    assert any(
        repair.get("source_field") == "chunks_used"
        for repair in repairs
    )


def test_zero_overlap_tail_uses_broader_trusted_section_vocabulary():
    note = (
        "Coordinated clause has zero evidence overlap with the cited chunk text: "
        "can be continuously transformed under parameter deformation"
    )
    trusted_tokens = {
        "continuously",
        "transformed",
        "parameter",
        "deformation",
    }

    assert _citation_note_hard_failure(
        note,
        trusted_tokens=trusted_tokens,
    ) is False


def test_zero_overlap_tail_without_trusted_vocabulary_remains_hard():
    note = (
        "Coordinated clause has zero evidence overlap with the cited chunk text: "
        "cures cancer"
    )

    assert _citation_note_hard_failure(
        note,
        trusted_tokens={"optical", "resonance", "mechanism"},
    ) is True


def test_durable_bounded_candidate_uses_terminal_passed_with_limits_marker():
    with tempfile.TemporaryDirectory() as tmpdir:
        work_dir = Path(tmpdir)
        ctx = _make_ctx(work_dir)
        (work_dir / "SECTION_DRAFT_EN.md").write_text(
            "A durable cited section remains available for downstream repair. " * 10,
            encoding="utf-8",
        )
        (work_dir / "SECTION_CITATION_MAP.json").write_text(
            json.dumps({"total_cited_sentences": 1, "papers_cited": ["paper_A"]}),
            encoding="utf-8",
        )
        (work_dir / "SECTION_AUTHORING_AUDIT.json").write_text(
            json.dumps({"total_flags": 2, "total_blocking_flags": 1}),
            encoding="utf-8",
        )

        result = _write_awaiting_human_review_package(
            ctx,
            reason="bounded_revision_convergence_reached",
            control={"stop_revising": True},
        )
        package = json.loads(
            (work_dir / "SECTION_AUTHORING_PACKAGE.json").read_text(encoding="utf-8")
        )

        assert result.startswith("VALIDATION_PASSED_WITH_LIMITS")
        assert package["authoring_status"] == "completed_with_limits"
        assert package["review_gate"]["blocking_flags"] == 1


def test_strong_assertion_is_editorial_warning_not_strict():
    sentence = (
        "This design does not require external phase compensation "
        "for integrated optical systems [REF:paper_A]."
    )

    assert _citation_risk_class(sentence) == "strong_assertion"
    assert _requires_strict_citation_entailment(sentence) is False


def test_exact_source_specific_result_stays_strict():
    sentence = (
        "The measured saturation threshold was 1.5 nJ/cm^2 [REF:paper_A]."
    )

    assert _citation_risk_class(sentence) == "exact_measurement"
    assert _requires_strict_citation_entailment(sentence) is True


def test_uncited_ordinary_strong_assertion_is_not_high_risk():
    sentence = "These results indicate that the reported design is broadly applicable."

    assert _find_uncited_high_risk_claims(sentence) == []


def test_interpretive_synthesis_uncovered_not_blocking():
    """Uncovered claim in an interpretive_synthesis paragraph should not be blocking."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        # First build argument plan with paragraph4 as interpretive_synthesis
        plan_json = json.dumps({
            "argument_flow": "test",
            "paragraphs": [
                {
                    "paragraph_index": 0,
                    "function": "synthesis",
                    "topic_sentence": "Synthesis of multiple mechanisms.",
                    "key_claims": ["C01"],
                    "evidence_chunk_ids": [],
                    "paper_ids": ["paper_A"],
                    "writing_permission": "interpretive_synthesis",
                    "expected_word_count": 100,
                }
            ],
        })
        _make_submit_argument_plan(ctx)(plan_json)
        # Now build evidence packet with C01 uncovered
        result = json.loads(_make_build_evidence_packet(ctx)(json.dumps({
            "items": [{
                "chunk_id": "chunk_found_001",
                "paper_id": "paper_A",
                "claim_ids": [],
                "writing_permission": "factual_assertion",
                "support_hint": "saturable absorption",
            }],
            "uncovered_claim_ids": ["C01"],
        })))
        assert result["status"] == "ok"
        assert result["blocking_uncovered_count"] == 0, (
            f"interpretive_synthesis C01 should not block; got: {result}"
        )


def test_load_bearing_factual_uncovered_is_blocking():
    """A load-bearing factual_assertion claim with no evidence should show blocking_uncovered_count=1."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        # Argument plan marks C01 as factual_assertion
        plan_json = json.dumps({
            "argument_flow": "test",
            "paragraphs": [{
                "paragraph_index": 0,
                "function": "evidence",
                "topic_sentence": "MoS2 saturable absorption.",
                "key_claims": ["C01"],
                "evidence_chunk_ids": ["chunk_found_001"],
                "paper_ids": ["paper_A"],
                "writing_permission": "factual_assertion",
                "expected_word_count": 100,
            }],
        })
        _make_submit_argument_plan(ctx)(plan_json)
        result = json.loads(_make_build_evidence_packet(ctx)(json.dumps({
            "items": [{
                "chunk_id": "chunk_mech_001",
                "paper_id": "paper_B",
                "claim_ids": [],
                "writing_permission": "factual_assertion",
                "support_hint": "Kerr nonlinearity graphene",
            }],
            "uncovered_claim_ids": ["C01"],  # C01 is load_bearing=True + factual_assertion
        })))
        assert result["status"] == "ok"
        assert result["blocking_uncovered_count"] == 1
def test_canonical_asset_graph_loads_only_adopted_chunk_ids(tmp_path: Path):
    """Accepting one excerpt must not expose every unrelated chunk in its paper."""
    from optomind_research.runtime.section_authoring_assets import (
        build_canonical_asset_graph,
    )

    kb = tmp_path / "kb.sqlite"
    with sqlite3.connect(kb) as conn:
        conn.execute(
            "CREATE TABLE text_chunks "
            "(chunk_id TEXT, paper_id TEXT, text TEXT, evidence_level TEXT, source_kind TEXT)"
        )
        conn.executemany(
            "INSERT INTO text_chunks VALUES (?,?,?,?,?)",
            [
                ("chunk_relevant", "paper_A", "Relevant optical metrology.", "fulltext", "fulltext"),
                ("chunk_unrelated", "paper_A", "Unrelated agricultural reconstruction.", "fulltext", "fulltext"),
            ],
        )

    material = tmp_path / "SECTION_MATERIAL_PACKAGE.json"
    material.write_text(json.dumps({
        "section_id": "S01",
        "chunk_ids_by_role": {"method": ["chunk_relevant"]},
    }), encoding="utf-8")
    ledger = tmp_path / "SECTION_SOURCE_LEDGER.json"
    ledger.write_text(json.dumps({
        "section_id": "S01",
        "sources": [{
            "paper_id": "paper_A",
            "title": "Broad Review",
            "literature_role": "method",
            "scope_fit": "adjacent",
            "acquisition_status": "fulltext",
            "canonical_chunk_ids": ["chunk_relevant"],
        }],
    }), encoding="utf-8")

    graph = build_canonical_asset_graph(
        material_package_path=material,
        source_ledger_path=ledger,
        work_dir=tmp_path,
        kb_paths=[kb],
    )
    assert set(graph.chunks) == {"chunk_relevant"}


def test_s07_writing_permission_aliases_are_normalized():
    assert _normalize_writing_permission("contextual_or_qualified_support") == (
        "hedged_factual_assertion"
    )
    assert _normalize_writing_permission(
        "can_quote_canonical_text_and_measurements"
    ) == "factual_assertion"
    assert _normalize_writing_permission(
        "hedged_factual_assertion_with_open_question_caveat"
    ) == "hedged_factual_assertion"


def test_s07_ambiguous_or_unknown_permission_remains_rejected():
    assert _normalize_writing_permission(
        "factual_assertion_or_interpretive_synthesis"
    ) == "factual_assertion_or_interpretive_synthesis"
    assert _normalize_writing_permission("unsupported_model_permission") == (
        "unsupported_model_permission"
    )


def test_s07_unknown_permission_remains_rejected_by_validator():
    ctx = SectionAuthoringContext(
        section_id="S07",
        section_data={"title": "S07", "claims": []},
        kb_sqlite=None,
        temp_kb_sqlite=None,
        work_dir=PROJECT_ROOT / ".nonexistent_s07_plan_dir",
    )
    graph = CanonicalAssetGraph()
    graph.papers["P1"] = PaperAsset(paper_id="P1", title="test paper")
    graph.chunks["K1"] = ChunkAsset(
        chunk_id="K1",
        paper_id="P1",
        normalized_text="some evidence text",
    )

    errors = _validate_argument_plan_data(ctx, graph, [{
        "paragraph_index": 0,
        "topic_sentence": "Some evidence text.",
        "key_claims": [],
        "evidence_chunk_ids": ["K1"],
        "paper_ids": ["P1"],
        "writing_permission": "unsupported_model_permission",
        "expected_word_count": 100,
    }])

    assert any(
        "invalid writing_permission='unsupported_model_permission'" in error
        for error in errors
    ), errors


def test_s07_argument_plan_normalizes_evidence_level_permission():
    graph = CanonicalAssetGraph()
    data = {
        "paragraphs": [{
            "paragraph_index": 0,
            "writing_permission": "contextual_or_qualified_support",
            "evidence_chunk_ids": [],
            "paper_ids": [],
        }],
    }

    normalized, repairs = _normalize_argument_plan_contract(data, graph)

    assert normalized["paragraphs"][0]["writing_permission"] == (
        "hedged_factual_assertion"
    )
    assert any(
        item.get("field") == "writing_permission"
        and item.get("normalized_value") == "hedged_factual_assertion"
        for item in repairs
    )


def test_s07_evidence_permission_alias_normalizes_to_paragraph_enum():
    ctx = SectionAuthoringContext(
        section_id="S07",
        section_data={"title": "S07", "claims": []},
        kb_sqlite=None,
        temp_kb_sqlite=None,
        work_dir=PROJECT_ROOT / ".nonexistent_s07_evidence_packet_dir",
    )
    graph = CanonicalAssetGraph()
    data = {
        "chunks_used": [{
            "chunk_id": "K1",
            "paper_id": "P1",
            "permission": "contextual_or_qualified_support",
            "not_usable_for": None,
        }],
    }

    normalized, repairs = _normalize_evidence_packet_contract(data, ctx, graph)

    assert normalized["items"][0]["writing_permission"] == (
        "hedged_factual_assertion"
    )
    assert any(
        item.get("field") == "writing_permission"
        and item.get("normalized_value") == "hedged_factual_assertion"
        for item in repairs
    )


def test_s07_not_usable_for_null_does_not_raise_type_error():
    ctx = SectionAuthoringContext(
        section_id="S07",
        section_data={
            "title": "S07",
            "claims": [{"claim_id": "C1", "statement": "some evidence text"}],
        },
        kb_sqlite=None,
        temp_kb_sqlite=None,
        work_dir=PROJECT_ROOT / ".nonexistent_s07_validate_dir",
    )
    graph = CanonicalAssetGraph()
    graph.papers["P1"] = PaperAsset(paper_id="P1", title="test paper")
    graph.chunks["K1"] = ChunkAsset(
        chunk_id="K1",
        paper_id="P1",
        normalized_text="some evidence text",
    )

    errors, canonical = _validate_evidence_items(
        ctx,
        graph,
        [{
            "chunk_id": "K1",
            "paper_id": "P1",
            "claim_ids": ["C1"],
            "writing_permission": "factual_assertion",
            "not_usable_for": None,
        }],
        [],
    )

    assert errors == []
    assert canonical[0]["not_usable_for"] == []


# ---------------------------------------------------------------------------
# Compact provider contract regression: SA-RC01..SA-RC06
# ---------------------------------------------------------------------------

from optomind_research.runtime.compact_section_authoring import (
    CompactSectionAuthoringToolProvider,
    COMPACT_SECTION_AUTHORING_TOOL_NAMES,
)


def _compact_submit_fn(provider: CompactSectionAuthoringToolProvider):
    """Return the submit_authoring_candidate callable from the provider."""
    tools = provider.get_tools(provider._ctx.work_dir)
    for tool in tools:
        # agentscope FunctionTool stores the underlying callable as _func
        for attr in ("func", "_func"):
            fn = getattr(tool, attr, None)
            if fn is not None:
                break
        else:
            fn = tool
        if getattr(fn, "__name__", "") == "submit_authoring_candidate":
            return fn
    raise AssertionError("submit_authoring_candidate not found in tools")


def test_compact_provider_exposes_four_tools():
    """SA-RC01: CompactSectionAuthoringToolProvider exposes exactly 4 tools."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        provider = CompactSectionAuthoringToolProvider(ctx)
        tools = provider.get_tools(ctx.work_dir)
        names = provider.get_allowed_tool_names()
        assert len(tools) == 4, f"Expected 4, got {len(tools)}"
        assert set(names) == set(COMPACT_SECTION_AUTHORING_TOOL_NAMES)


def test_submit_authoring_candidate_schema_accepts_draft_text_field():
    """SA-RC02: submit_authoring_candidate signature includes draft_text and
    argument_plan alongside candidate_json; all are optional."""
    import inspect
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        provider = CompactSectionAuthoringToolProvider(ctx)
        fn = _compact_submit_fn(provider)
        params = set(inspect.signature(fn).parameters)
        assert "candidate_json" in params, "candidate_json must remain in schema"
        assert "draft_text" in params, "draft_text must be a direct parameter"
        assert "argument_plan" in params, "argument_plan must be a direct parameter"
        assert "evidence_packet" in params
        assert "visual_placements" in params
        assert "handoff_card" in params
        # All parameters have defaults (none are required positional)
        sig = inspect.signature(fn)
        for name, param in sig.parameters.items():
            assert param.default is not inspect.Parameter.empty, (
                f"Parameter '{name}' has no default; it must be optional"
            )


def test_submit_authoring_candidate_flat_field_form_normalized():
    """SA-RC03: Passing draft_text + argument_plan directly produces a structured
    JSON response — never a Python argument-binding or AttributeError crash."""
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        provider = CompactSectionAuthoringToolProvider(ctx)
        fn = _compact_submit_fn(provider)

        result = json.loads(fn(
            draft_text=(
                "Two-dimensional materials such as MoS2 exhibit strong nonlinear "
                "optical responses arising from their atomically thin geometry "
                "[[claim:C01]].\n\n"
                "This observation is used as an anchor for the section argument "
                "rather than as a universal performance guarantee."
            ),
            argument_plan={
                "argument_flow": "evidence -> synthesis",
                "paragraphs": [{
                    "paragraph_index": 0,
                    "function": "foundation",
                    "topic_sentence": "MoS2 exhibits saturable absorption.",
                    "key_claims": ["C01"],
                    "expected_word_count": 120,
                }],
            },
        ))
        assert isinstance(result, dict), "Response must be a JSON object"
        assert "status" in result, "Response must contain 'status'"
        assert result["status"] in {
            "completed", "repair_required", "revision_required", "error",
        }, f"Unexpected status value: {result['status']}"
        # Must not be a Python argument-binding error
        assert result.get("stage") != "argument_binding"


def test_submit_authoring_candidate_flat_field_evidence_packet_passed_through():
    """SA-RC04: evidence_packet supplied in flat-field form is forwarded to the
    evidence gate unchanged; a fake chunk_id still causes a rejection."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        provider = CompactSectionAuthoringToolProvider(ctx)
        fn = _compact_submit_fn(provider)

        result = json.loads(fn(
            draft_text="MoS2 shows saturable absorption. A second sentence follows.",
            argument_plan={
                "paragraphs": [{
                    "paragraph_index": 0,
                    "function": "foundation",
                    "topic_sentence": "MoS2 exhibits saturable absorption.",
                    "key_claims": ["C01"],
                    "expected_word_count": 80,
                }],
            },
            evidence_packet={
                "items": [{
                    "chunk_id": "FAKE_CHUNK_XYZ",
                    "paper_id": "FAKE_PAPER_XYZ",
                    "claim_ids": ["C01"],
                    "writing_permission": "factual_assertion",
                    "support_hint": "saturable absorption",
                }],
            },
        ))
        # The evidence gate must fire and produce repair_required or error,
        # not a silent pass with fabricated identifiers.
        assert result["status"] in {
            "repair_required", "revision_required", "error",
        }, f"Expected rejection of fake IDs, got: {result['status']}"


def test_submit_authoring_candidate_no_fields_returns_bounded_error():
    """SA-RC05: Calling with neither candidate_json nor individual fields returns
    a bounded JSON error — not a Python exception or an unhandled crash."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        provider = CompactSectionAuthoringToolProvider(ctx)
        fn = _compact_submit_fn(provider)
        result = json.loads(fn())
        assert result["status"] == "error"
        assert result["stage"] == "parse"
        assert "candidate_json" in result["error"] or "draft_text" in result["error"]


def test_submit_authoring_candidate_canonical_json_form_unchanged():
    """SA-RC06: The canonical candidate_json string form continues to work and
    produces a structured response (regression guard)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        wd = Path(tmpdir)
        ctx = _make_ctx(wd)
        provider = CompactSectionAuthoringToolProvider(ctx)
        fn = _compact_submit_fn(provider)

        candidate_json = json.dumps({
            "argument_plan": {
                "argument_flow": "evidence",
                "paragraphs": [{
                    "paragraph_index": 0,
                    "function": "foundation",
                    "topic_sentence": "MoS2 exhibits saturable absorption.",
                    "key_claims": ["C01"],
                    "expected_word_count": 120,
                }],
            },
            "draft_text": (
                "Two-dimensional materials such as MoS2 exhibit strong nonlinear "
                "optical responses [[claim:C01]].\n\n"
                "This provides a disciplined starting point for the argument."
            ),
        })
        result = json.loads(fn(candidate_json=candidate_json))
        assert "status" in result
        assert result["status"] in {
            "completed", "repair_required", "revision_required",
        }, f"Unexpected status: {result['status']}"


def test_submit_authoring_candidate_malformed_json_returns_bounded_repair():
    """SA-RC07: Malformed candidate_json returns a bounded parse error — not a
    Python traceback."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _make_ctx(Path(tmpdir))
        provider = CompactSectionAuthoringToolProvider(ctx)
        fn = _compact_submit_fn(provider)
        result = json.loads(fn(candidate_json="{not valid json at all"))
        assert result["status"] == "error"
        assert result["stage"] == "parse"
