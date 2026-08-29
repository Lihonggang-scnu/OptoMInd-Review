"""Domain-neutral adversarial tests for the R4 Phase-3 authoring bridge.

These tests deliberately use synthetic scientific records.  They do not call
Qwen, Semantic Scholar, or a network service; they test the production trust
boundary and writing-policy behavior only.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory."""

    root = (
        Path(__file__).resolve().parents[1]
        / ".pytest-basetemp-r4-phase3"
    )
    root.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)[:40]
    path = root / f"{safe_name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_phase3_root(tmp_path: Path) -> Path:
    root = tmp_path / "phase3"
    ledger_path = root / "coverage_snapshot" / "sections" / "S01" / "SECTION_SOURCE_LEDGER.json"
    overlay_path = root / "coverage_requests" / "S01" / "SECTION_ASSET_OVERLAY.json"
    kb_path = root / "shared.sqlite"
    _write_json(ledger_path, {
        "section_id": "S01",
        "sources": [
            {
                "paper_id": "p_direct",
                "title": "Direct study",
                "literature_role": "mechanism",
                "scope_fit": "direct",
                "canonical_chunk_ids": ["c_direct"],
                "acquisition_status": "fulltext",
                "content_depth": "fulltext",
                "context_complete": True,
                "use_permission": "factual_support",
                "discovery_route": "phase3_test",
                "materialization_route": "oa_pdf",
            },
            {
                "paper_id": "p_discovery",
                "title": "Discovery lead",
                "literature_role": "frontier",
                "scope_fit": "direct",
                "canonical_chunk_ids": ["c_discovery"],
                "acquisition_status": "metadata_only",
                "content_depth": "metadata",
                "context_complete": False,
                "use_permission": "discovery_only",
                "discovery_route": "phase3_test",
                "materialization_route": "not_materialized",
            },
        ],
    })
    _write_json(overlay_path, {"section_id": "S01", "paper_ids": ["p_direct", "p_discovery"]})
    conn = sqlite3.connect(str(kb_path))
    try:
        conn.execute(
            "CREATE TABLE text_chunks ("
            "chunk_id TEXT, paper_id TEXT, title TEXT, text TEXT, "
            "evidence_level TEXT, source_kind TEXT, discovery_route TEXT, "
            "content_depth TEXT, context_complete INTEGER, use_permission TEXT, "
            "route_provenance_json TEXT, scope_fit TEXT)"
        )
        conn.execute(
            "CREATE TABLE visual_chunks ("
            "visual_chunk_id TEXT, paper_id TEXT, caption TEXT, local_image_path TEXT, "
            "chunk_kind TEXT, visual_argument_type TEXT, visual_argument_status TEXT, "
            "relevance_status TEXT, status TEXT)"
        )
        provenance = json.dumps({"migration": "r3_2", "use_permission": "factual_support"})
        conn.execute(
            "INSERT INTO text_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "c_direct", "p_direct", "Direct study",
                "The direct study reports a bounded mechanism under the tested conditions.",
                "fulltext", "fulltext", "phase3_test", "fulltext", 1,
                "factual_support", provenance, "direct",
            ),
        )
        conn.execute(
            "INSERT INTO text_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "c_discovery", "p_discovery", "Discovery lead",
                "A discovery record contains only a title-level lead.",
                "metadata", "metadata", "phase3_test", "metadata", 0,
                "discovery_only", json.dumps({"migration": "r3_2"}), "direct",
            ),
        )
        conn.execute(
            "INSERT INTO visual_chunks VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "v_direct", "p_direct", "A mechanism schematic", "missing.png",
                "single_figure", "mechanism_anchor", "ok", "direct", "ok",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    claims = {
        "CQUAL": {
            "claim_id": "CQUAL",
            "statement": "The mechanism is established across all operating conditions.",
            "effective_statement": "Boettcher, Real spectra in non-Hermitian Hamiltonians, Phys. Rev.",
            "supported_rewrite": "Boettcher, Real spectra in non-Hermitian Hamiltonians, Phys. Rev.",
            "evidence_binding_status": "partial",
            "permission_status": "qualified_only",
            "claim_state": "partially_grounded",
            "supporting_chunk_ids": ["c_direct"],
            "factual_support_chunk_ids": ["c_direct"],
            "contextual_support_chunk_ids": [],
            "core_chunk_ids": ["c_direct"],
            "core_paper_ids": ["p_direct"],
            "missing_evidence_components": ["conditions outside the tested regime"],
        },
        "COPEN": {
            "claim_id": "COPEN",
            "statement": "The transfer to the untested regime remains unresolved.",
            "effective_statement": "Figure 2: unresolved transfer curve.",
            "supported_rewrite": "Figure 2: unresolved transfer curve.",
            "evidence_binding_status": "open_question",
            "permission_status": "qualified_only",
            "claim_state": "open_question",
            "supporting_chunk_ids": [],
            "factual_support_chunk_ids": [],
            "contextual_support_chunk_ids": [],
            "core_chunk_ids": [],
            "core_paper_ids": [],
            "missing_evidence_components": ["independent validation"],
        },
        "CDISC": {
            "claim_id": "CDISC",
            "statement": "The discovery lead establishes the deployment performance.",
            "effective_statement": "As reported in the arXiv header, deployment performance...",
            "supported_rewrite": "As reported in the arXiv header, deployment performance...",
            "evidence_binding_status": "partial",
            "permission_status": "discovery_only",
            "claim_state": "partially_grounded",
            "supporting_chunk_ids": ["c_discovery"],
            "factual_support_chunk_ids": [],
            "contextual_support_chunk_ids": ["c_discovery"],
            "core_chunk_ids": [],
            "core_paper_ids": [],
        },
    }
    _write_json(root / "COVERAGE_ATLAS.json", {
        "schema_version": "test",
        "sections": [{"section_id": "S01", "unique_papers": 2, "needs_expansion": True}],
        "source": {
            "section_ledgers": str(root / "coverage_snapshot" / "sections"),
            "shared_kb_paths": [str(kb_path)],
            "overlay_paths": {"S01": str(overlay_path)},
        },
    })
    _write_json(root / "SYNTHESIS_BUNDLES.json", {
        "bundles": [{
            "section_id": "S01", "paper_ids": ["p_direct", "p_discovery"],
            "chunk_ids": ["c_direct"], "readiness_status": "ready_for_authoring",
            "status": "material_ready", "visual_chunk_ids": ["v_direct"],
            "claim_category_assignments": [],
        }]
    })
    _write_json(root / "MATERIAL_BINDINGS.json", {
        "sections": {"S01": {"section_id": "S01", "claims": claims, "status": "material_ready"}}
    })
    _write_json(root / "CLAIM_GRAPH.json", {
        "claims": list(claims.values()), "edges": [], "status": "ok"
    })
    _write_json(root / "RELATION_GRAPH_MIGRATED.json", {"edges": []})
    return root


def _make_blueprint(tmp_path: Path) -> Path:
    path = tmp_path / "blueprint.json"
    _write_json(path, {
        "schema_version": "research_harness.review_blueprint.v2",
        "sections": [{
            "section_id": "S01", "title": "A bounded mechanism",
            "argument_role": "mechanism", "chapter_argument": "Explain the mechanism.",
            "claims": [], "section_contract": {}, "visual_argument_slots": [],
        }],
    })
    return path


def _make_context(tmp_path: Path):
    from optomind_research.runtime.full_review_orchestrator import (
        FullReviewOrchestrator,
        OrchestratorConfig,
    )

    phase3 = _make_phase3_root(tmp_path)
    orchestrator = FullReviewOrchestrator(OrchestratorConfig(
        blueprint_path=_make_blueprint(tmp_path),
        output_root=tmp_path / "out",
        phase3_artifacts_root=phase3,
        phase3_handoff_mode="legacy_migration",
    ))
    orchestrator._work_dir = tmp_path / "run"
    orchestrator._work_dir.mkdir(parents=True, exist_ok=True)
    section = {"section_id": "S01", "title": "A bounded mechanism", "argument_role": "mechanism"}
    ctx = orchestrator._build_section_context(
        section, None, None, {"sections": [section]}, tmp_path / "run" / "sections" / "S01"
    )
    return ctx


def test_r4_phase3_store_normalizes_claims_and_strengths(tmp_path: Path):
    from optomind_research.runtime.r4_phase3_artifacts import R4Phase3ArtifactStore

    artifacts = R4Phase3ArtifactStore.from_legacy(_make_phase3_root(tmp_path)).section("S01")
    assert {item["claim_id"] for item in artifacts.judgment_ledger} == {"CQUAL", "COPEN", "CDISC"}
    strengths = {item["claim_id"]: item["strength"] for item in artifacts.judgment_ledger}
    assert strengths["CQUAL"] == "qualified"
    assert strengths["COPEN"] == "open"
    assert strengths["CDISC"] == "qualified"
    assert artifacts.visual_chunk_ids == ["v_direct"]
    assert artifacts.source_ledger_path and artifacts.kb_paths


def test_r4_exact_real_binding_aliases_and_rewrites_are_separated(tmp_path: Path):
    from optomind_research.runtime.r4_phase3_artifacts import R4Phase3ArtifactStore

    artifacts = R4Phase3ArtifactStore.from_legacy(_make_phase3_root(tmp_path)).section("S01")
    rows = {item["claim_id"]: item for item in artifacts.judgment_ledger}
    assert rows["CQUAL"]["supporting_text_chunk_ids"] == ["c_direct"]
    assert rows["CQUAL"]["factual_support_chunk_ids"] == ["c_direct"]
    assert rows["CQUAL"]["core_chunk_ids"] == ["c_direct"]
    assert rows["CQUAL"]["core_paper_ids"] == ["p_direct"]
    assert "mechanism is established" in rows["CQUAL"]["statement"]
    assert "Boettcher" not in rows["CQUAL"]["statement"]
    assert "Boettcher" in rows["CQUAL"]["bounded_evidence_paraphrase"]
    assert artifacts.handoff_audit["status"] == "pass_with_warnings"
    assert artifacts.handoff_audit["live_authoring_allowed"] is True
    assert any(
        "bibliography_like" in item["flag"]
        for item in artifacts.handoff_audit["warnings"]
    )


def test_r4_handoff_audit_flags_caption_header_and_incompatible_excerpt():
    from optomind_research.runtime.r4_phase3_artifacts import (
        audit_r4_handoff_quality,
        build_judgment_ledger,
    )

    claims = [{
        "claim_id": "C1",
        "statement": "The resonant cavity increases field confinement under the tested boundary conditions.",
        "effective_statement": "Figure 2: arXiv:2104.06929v2 [quant-ph] 12 Jul 2021. Boettcher, Phys. Rev.",
        "evidence_binding_status": "partial",
        "permission_status": "qualified_only",
        "claim_state": "partially_grounded",
        "supporting_chunk_ids": ["chunk-1"],
    }]
    ledger = build_judgment_ledger(claims)
    audit = audit_r4_handoff_quality(claims, ledger)
    assert audit["status"] == "pass_with_warnings"
    assert audit["live_authoring_allowed"] is True
    flags = {item["flag"] for item in audit["warnings"]}
    assert any("caption_or_table_of_contents" in flag for flag in flags)
    assert any("arxiv_or_venue_header" in flag for flag in flags)
    assert any("bibliography_like" in flag for flag in flags)
    assert any("proposition_incompatible" in flag for flag in flags)
    assert ledger[0]["statement"].startswith("The resonant cavity")


def test_r4_handoff_blocks_contaminated_authored_statement():
    from optomind_research.runtime.r4_phase3_artifacts import (
        audit_r4_handoff_quality,
        build_judgment_ledger,
    )

    claims = [{
        "claim_id": "BAD",
        "statement": "Boettcher, Real spectra in non-Hermitian Hamiltonians, Phys. Rev.",
        "evidence_binding_status": "partial",
        "permission_status": "qualified_only",
        "claim_state": "partially_grounded",
        "supporting_chunk_ids": ["chunk-1"],
    }]
    ledger = build_judgment_ledger(claims)
    audit = audit_r4_handoff_quality(claims, ledger)
    assert audit["status"] == "blocked"
    assert audit["live_authoring_allowed"] is False
    assert any(
        item["flag"] == "authoring_statement:bibliography_like"
        for item in audit["blocking_flags"]
    )


def test_r4_claim_scoped_hard_flag_excludes_only_affected_claim():
    from optomind_research.runtime.r4_phase3_artifacts import (
        audit_r4_handoff_quality,
        build_judgment_ledger,
    )

    claims = [
        {
            "claim_id": "GOOD",
            "statement": "The bounded mechanism is established under the tested conditions.",
            "evidence_binding_status": "partial",
            "permission_status": "qualified_only",
            "claim_state": "partially_grounded",
            "supporting_chunk_ids": ["chunk-1"],
        },
        {
            "claim_id": "BAD",
            "statement": (
                "Boettcher, Real spectra in non-Hermitian Hamiltonians, "
                "Phys. Rev. Lett. 120, 011 (2018)."
            ),
            "evidence_binding_status": "bound",
            "permission_status": "bound",
            "claim_state": "grounded",
            "supporting_chunk_ids": ["chunk-1"],
        },
    ]
    ledger = build_judgment_ledger(claims)
    audit = audit_r4_handoff_quality(claims, ledger)
    assert audit["status"] == "pass_with_limits"
    assert audit["live_authoring_allowed"] is True
    assert audit["excluded_claim_ids"] == ["BAD"]
    assert audit["authorable_claim_ids"] == ["GOOD"]
    assert {item["claim_id"] for item in audit["excluded_claims"]} == {"BAD"}
    assert any(
        item["claim_id"] == "BAD"
        and item["flag"] == "authoring_statement:bibliography_like"
        for item in audit["blocking_flags"]
    )


def test_r4_open_or_unsupported_claim_never_receives_factual_permission():
    from optomind_research.runtime.r4_phase3_artifacts import (
        build_judgment_ledger,
    )

    claims = [{
        "claim_id": "OPEN",
        "statement": "The transfer to the untested regime remains unresolved.",
        "evidence_binding_status": "unbound",
        "permission_status": "unbound",
        "claim_state": "open_question",
        "support_classification": "open_question",
        "supporting_chunk_ids": [],
    }]
    ledger = build_judgment_ledger(claims)
    assert ledger[0]["strength"] == "open"
    assert ledger[0]["writing_permission"] == "evidence_gap_only"


def test_r4_supported_claim_with_deferred_formal_verification_is_not_gap_only():
    from optomind_research.runtime.r4_phase3_artifacts import build_judgment_ledger

    ledger = build_judgment_ledger([{
        "claim_id": "SUPPORTED",
        "statement": "The measured mechanism is supported within the reported regime.",
        "evidence_binding_status": "unverified",
        "permission_status": "bound",
        "claim_state": "planned",
        "support_classification": "supported",
        "critic_flags": ["formal_verification_deferred"],
        "supporting_chunk_ids": ["chunk-1"],
        "factual_support_chunk_ids": ["chunk-1"],
        "missing_evidence_components": [],
    }])

    assert ledger[0]["strength"] == "qualified"
    assert ledger[0]["writing_permission"] == "hedged_factual_assertion"


def test_r4_open_prose_with_comma_and_optics_is_not_bibliography():
    from optomind_research.runtime.r4_phase3_artifacts import (
        _handoff_text_flags,
        build_judgment_ledger,
    )

    text = (
        "Open question: the available material does not establish whether "
        "integrated waveguides enable compact, chip-scale devices with "
        "inherent compatibility for nonlinear optics and lasing applications."
    )
    flags = _handoff_text_flags(text)
    assert "bibliography_like" not in flags
    assert "empty_text" not in flags

    ledger = build_judgment_ledger([{
        "claim_id": "OPEN",
        "statement": text,
        "evidence_binding_status": "unbound",
        "permission_status": "unbound",
        "claim_state": "open_question",
        "support_classification": "open_question",
        "supporting_chunk_ids": [],
    }])
    assert ledger[0]["statement_integrity_flags"] == []
    assert ledger[0]["strength"] == "open"
    assert ledger[0]["writing_permission"] == "evidence_gap_only"


def test_r4_genuine_bibliography_entries_remain_flagged():
    from optomind_research.runtime.r4_phase3_artifacts import (
        _handoff_text_flags,
    )

    for text in (
        "Boettcher, Real spectra in non-Hermitian Hamiltonians, Phys. Rev.",
        (
            "Boettcher, Real spectra in non-Hermitian Hamiltonians, "
            "Phys. Rev. Lett. 120, 011 (2018)."
        ),
        "Boettcher, Phys. Rev.",
        (
            "Doe, J., Smith, A. B., Nonlinear optics in photonic crystals, "
            "Opt. Lett. 45, 012 (2020)."
        ),
    ):
        assert "bibliography_like" in _handoff_text_flags(text), text


def _make_canonical_limits_root(
    tmp_path: Path, *, only_bad: bool = False
) -> Path:
    from optomind_research.runtime.r3_production_handoff import (
        build_r3_production_handoff,
        write_r3_production_handoff,
    )

    root = tmp_path / "phase3"
    topic = {"topic_id": "neutral-mechanism-topic"}
    good = {
        "claim_id": "S01:GOOD",
        "section_id": "S01",
        "statement": "The bounded mechanism controls the measured response.",
        "criticality": "supporting",
        "claim_state": "grounded",
        "evidence_type": "mechanism",
        "support_classification": "supported",
        "evidence_binding_status": "bound",
        "permission_status": "bound",
        "supporting_chunk_ids": ["K01"],
        "factual_support_chunk_ids": ["K01"],
        "core_chunk_ids": ["K01"],
        "core_paper_ids": ["P01"],
    }
    bad = {
        "claim_id": "S01:BAD",
        "section_id": "S01",
        "statement": (
            "Boettcher, Real spectra in non-Hermitian Hamiltonians, "
            "Phys. Rev. Lett. 120, 011 (2018)."
        ),
        "criticality": "optional",
        "claim_state": "open_question",
        "evidence_type": "mechanism",
        "support_classification": "open_question",
        "evidence_binding_status": "unbound",
        "permission_status": "unbound",
        "supporting_chunk_ids": [],
    }
    bindings = {
        "S01": {
            "section_id": "S01",
            "claims": {
                "S01:GOOD": {
                    "claim_id": "S01:GOOD",
                    "evidence_binding_status": "bound",
                    "permission_status": "bound",
                    "write_status": "bound",
                    "supporting_chunk_ids": ["K01"],
                    "factual_support_chunk_ids": ["K01"],
                    "paper_ids": ["P01"],
                    "support_classification": "supported",
                },
                "S01:BAD": {
                    "claim_id": "S01:BAD",
                    "evidence_binding_status": "unbound",
                    "permission_status": "unbound",
                    "write_status": "write_with_declared_gap",
                    "supporting_chunk_ids": [],
                    "paper_ids": [],
                    "support_classification": "open_question",
                },
            },
        }
    }
    if only_bad:
        del bindings["S01"]["claims"]["S01:GOOD"]
    handoff = build_r3_production_handoff(
        topic_identity=topic,
        sections=[{
            "section_id": "S01",
            "title": "Bounded mechanism",
            "topic_identity": dict(topic),
        }],
        coverage_atlas={
            "schema_version": "research_harness.coverage_atlas.v1",
            "topic_identity": dict(topic),
            "sections": [{"section_id": "S01", "needs_expansion": True}],
            "relation_graph": {"edge_count": 0},
        },
        section_argument_contracts={
            "S01": {
                "schema_version": "research_harness.section_argument_contract.v1",
                "section_id": "S01",
                "status": "contract_ready",
                "argument_tasks": [{
                    "task_id": "S01:T01",
                    "description": "Explain the bounded mechanism.",
                }],
            }
        },
        claims_by_criticality={
            "load_bearing": [],
            "supporting": [] if only_bad else [good],
            "optional": [bad],
        },
        material_inventory={
            "papers": {
                "P01": {
                    "paper_id": "P01",
                    "scope_fit": "direct",
                    "use_permission": "factual_support",
                    "content_depth": "fulltext",
                    "context_complete": True,
                }
            },
            "chunks": {
                "K01": {
                    "chunk_id": "K01",
                    "paper_id": "P01",
                    "scope_fit": "direct",
                    "use_permission": "factual_support",
                    "content_depth": "fulltext",
                    "context_complete": True,
                    "source_kind": "fulltext",
                }
            },
            "visuals": {},
        },
        material_bindings=bindings,
        relation_graph={"schema_version": "r3.relation_graph.v1", "edges": []},
        claim_dag={"schema_version": "research_harness.claim_graph.v1", "edges": []},
        gaps=[],
        coverage_requests=[],
        synthesis_bundles={
            "S01": {
                "section_id": "S01",
                "status": "needs_more_literature",
                "section_outcome": "needs_more_literature",
                "readiness_status": "needs_more_literature",
                "paper_ids": ["P01"],
                "chunk_ids": ["K01"],
                "claim_category_assignments": [
                    {"claim_id": "S01:GOOD", "category": "established_points"},
                    {"claim_id": "S01:BAD", "category": "open_questions"},
                ] if not only_bad else [
                    {"claim_id": "S01:BAD", "category": "open_questions"},
                ],
            }
        },
        visual_bindings={"S01": []},
        visual_needs={"S01": []},
    )
    write_r3_production_handoff(
        root / "R3_PRODUCTION_HANDOFF.json",
        handoff,
        fail_on_invalid=True,
    )
    ledger_path = root / "coverage_snapshot" / "sections" / "S01" / "SECTION_SOURCE_LEDGER.json"
    _write_json(ledger_path, {
        "section_id": "S01",
        "sources": [{
            "paper_id": "P01",
            "title": "Bounded mechanism study",
            "scope_fit": "direct",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
        }],
    })
    (root / "shared_kb.sqlite").write_bytes(b"sqlite-placeholder")
    return root


def test_r4_canonical_limits_admission_excludes_only_bad_claim(tmp_path: Path):
    from optomind_research.runtime.r4_phase3_artifacts import R4Phase3ArtifactStore

    root = _make_canonical_limits_root(tmp_path)
    artifacts = R4Phase3ArtifactStore(root).section("S01")
    assert artifacts.production_handoff_valid is True
    assert artifacts.ready_for_authoring is False
    assert artifacts.authorable_with_limits is True
    assert artifacts.admitted_for_authoring is True
    assert artifacts.excluded_claim_ids == ["S01:BAD"]
    assert artifacts.authorable_claim_ids == ["S01:GOOD"]

    payload = artifacts.to_context_payload()
    assert [item["claim_id"] for item in payload["claims"]] == ["S01:GOOD"]
    assert [item["claim_id"] for item in payload["judgment_ledger"]] == ["S01:GOOD"]
    assert [
        item["claim_id"]
        for item in payload["synthesis_bundle"]["claim_category_assignments"]
    ] == ["S01:GOOD"]
    assert payload["excluded_claim_ids"] == ["S01:BAD"]
    assert [item["claim_id"] for item in payload["excluded_claims"]] == ["S01:BAD"]
    assert all(
        item["writing_permission"] != "factual_assertion"
        or item["claim_id"] == "S01:GOOD"
        for item in payload["claims"]
    )


def test_r4_missing_source_ownership_closes_limits_admission(tmp_path: Path):
    from optomind_research.runtime.r4_phase3_artifacts import R4Phase3ArtifactStore

    root = _make_canonical_limits_root(tmp_path)
    ledger = root / "coverage_snapshot" / "sections" / "S01" / "SECTION_SOURCE_LEDGER.json"
    ledger.unlink()
    artifacts = R4Phase3ArtifactStore(root).section("S01")
    assert artifacts.production_handoff_valid is True
    assert artifacts.ready_for_authoring is False
    assert artifacts.authorable_with_limits is False
    assert artifacts.admitted_for_authoring is False
    assert "section_source_ledger_not_found" in artifacts.diagnostics


def test_r4_missing_kb_closes_limits_admission(tmp_path: Path):
    from optomind_research.runtime.r4_phase3_artifacts import R4Phase3ArtifactStore

    root = _make_canonical_limits_root(tmp_path)
    (root / "shared_kb.sqlite").unlink()
    artifacts = R4Phase3ArtifactStore(root).section("S01")
    assert artifacts.production_handoff_valid is True
    assert artifacts.ready_for_authoring is False
    assert artifacts.authorable_with_limits is False
    assert artifacts.admitted_for_authoring is False
    assert "phase3_kb_not_found" in artifacts.diagnostics


def test_r4_zero_authorable_claims_closes_admission(tmp_path: Path):
    from optomind_research.runtime.r4_phase3_artifacts import R4Phase3ArtifactStore

    root = _make_canonical_limits_root(tmp_path, only_bad=True)
    artifacts = R4Phase3ArtifactStore(root).section("S01")
    assert artifacts.production_handoff_valid is True
    assert artifacts.handoff_audit["status"] == "blocked"
    assert artifacts.handoff_audit["live_authoring_allowed"] is False
    assert artifacts.excluded_claim_ids == ["S01:BAD"]
    assert artifacts.authorable_claim_ids == []
    assert artifacts.authorable_with_limits is False
    assert artifacts.admitted_for_authoring is False


def test_r4_production_context_uses_phase3_not_blueprint_placeholders(tmp_path: Path):
    ctx = _make_context(tmp_path)
    assert {item["claim_id"] for item in ctx.section_data["claims"]} == {"CQUAL", "COPEN", "CDISC"}
    assert ctx.section_data["judgment_ledger"]
    assert ctx.section_data["visual_chunk_ids"] == ["v_direct"]
    assert ctx.section_overlay_path and ctx.section_overlay_path.exists()
    assert ctx.synthesis_bundle_path and ctx.synthesis_bundle_path.exists()

    from optomind_research.runtime.section_authoring_tool_registry import (
        _make_load_authoring_context,
    )

    payload = json.loads(_make_load_authoring_context(ctx)())
    assert payload["status"] == "ok"
    assert {item["claim_id"] for item in payload["judgment_ledger"]} == {"CQUAL", "COPEN", "CDISC"}
    assert payload["synthesis_bundle"]["status"] == "material_ready"
    assert "v_direct" in payload["visual_chunk_ids"]


def test_r4_strength_gate_rejects_qualified_claim_as_factual(tmp_path: Path):
    ctx = _make_context(tmp_path)
    from optomind_research.runtime.section_authoring_tool_registry import (
        _build_asset_graph,
        _validate_argument_plan_data,
    )

    graph = _build_asset_graph(ctx)
    errors = _validate_argument_plan_data(ctx, graph, [{
        "paragraph_index": 0,
        "topic_sentence": "The mechanism is established across all conditions.",
        "key_claims": ["CQUAL"],
        "evidence_chunk_ids": ["c_direct"],
        "paper_ids": ["p_direct"],
        "writing_permission": "factual_assertion",
        "expected_word_count": 100,
    }])
    assert any("CQUAL is qualified" in item for item in errors)


def test_r4_open_claim_can_remain_visible_without_blocking_conditional_writing(tmp_path: Path):
    ctx = _make_context(tmp_path)
    from optomind_research.runtime.section_authoring_tool_registry import (
        _build_asset_graph,
        _validate_argument_plan_data,
    )

    graph = _build_asset_graph(ctx)
    errors = _validate_argument_plan_data(ctx, graph, [{
        "paragraph_index": 0,
        "topic_sentence": "The transfer remains an open question for future work.",
        "key_claims": ["COPEN"],
        "evidence_chunk_ids": [],
        "paper_ids": [],
        "writing_permission": "evidence_gap_only",
        "expected_word_count": 100,
    }])
    assert not any("COPEN is open" in item for item in errors)


def test_r4_discovery_only_never_enters_factual_plan(tmp_path: Path):
    ctx = _make_context(tmp_path)
    from optomind_research.runtime.section_authoring_tool_registry import (
        _build_asset_graph,
        _validate_argument_plan_data,
    )

    graph = _build_asset_graph(ctx)
    errors = _validate_argument_plan_data(ctx, graph, [{
        "paragraph_index": 0,
        "topic_sentence": "The discovery lead establishes deployment performance.",
        "key_claims": ["CDISC"],
        "evidence_chunk_ids": ["c_discovery"],
        "paper_ids": ["p_discovery"],
        "writing_permission": "factual_assertion",
        "expected_word_count": 100,
    }])
    assert any("discovery_only" in item for item in errors)


def test_r4_accepts_legacy_list_word_range_in_blueprint(tmp_path: Path):
    from optomind_research.runtime.full_review_orchestrator import (
        FullReviewOrchestrator,
        OrchestratorConfig,
    )

    phase3 = _make_phase3_root(tmp_path)
    blueprint_path = _make_blueprint(tmp_path)
    blueprint = json.loads(blueprint_path.read_text(encoding="utf-8"))
    blueprint["sections"][0]["target_word_range"] = [900, 1400]
    blueprint_path.write_text(json.dumps(blueprint), encoding="utf-8")
    orchestrator = FullReviewOrchestrator(OrchestratorConfig(
        blueprint_path=blueprint_path,
        output_root=tmp_path / "out",
        phase3_artifacts_root=phase3,
        phase3_handoff_mode="legacy_migration",
    ))
    orchestrator._work_dir = tmp_path / "run"
    orchestrator._work_dir.mkdir(parents=True, exist_ok=True)
    ctx = orchestrator._build_section_context(
        blueprint["sections"][0], None, None, blueprint,
        tmp_path / "run" / "sections" / "S01",
    )
    assert ctx.section_data["estimated_word_budget"] == 1150


def test_r4_open_permission_is_downgraded_and_audited(tmp_path: Path):
    from optomind_research.runtime.section_authoring_tool_registry import (
        _make_load_authoring_context,
        _make_submit_argument_plan,
    )

    ctx = _make_context(tmp_path)
    _make_load_authoring_context(ctx)()
    result = json.loads(_make_submit_argument_plan(ctx)(json.dumps({
        "argument_flow": "Keep the unresolved transfer visible.",
        "paragraphs": [{
            "paragraph_index": 0,
            "function": "synthesis",
            "topic_sentence": "The transfer remains unresolved.",
            "key_claims": ["COPEN"],
            "evidence_chunk_ids": [],
            "paper_ids": [],
            "writing_permission": "hedged_factual_assertion",
            "expected_word_count": 100,
        }],
    })))
    assert result["status"] == "ok"
    assert result["permission_corrections"][0]["normalized_permission"] == "evidence_gap_only"
    plan = json.loads((ctx.work_dir / "SECTION_ARGUMENT_PLAN.json").read_text(encoding="utf-8"))
    assert plan["paragraphs"][0]["writing_permission"] == "evidence_gap_only"
    audit = json.loads(
        (ctx.work_dir / "SECTION_PERMISSION_CORRECTIONS.json").read_text(encoding="utf-8")
    )
    assert audit["count"] == 1


def test_r4_real_model_contract_is_compact_and_nonduplicative(tmp_path: Path):
    root = Path(__file__).resolve().parents[1] / "outputs" / "phase2_3_budget_fill_final_s01_20260802"
    blueprint_path = root / "S01_FEEDBACK_BLUEPRINT.json"
    if not root.exists() or not blueprint_path.exists():
        pytest.skip("real Phase-3 S01 artifact root is not available")

    from optomind_research.runtime.full_review_orchestrator import (
        FullReviewOrchestrator,
        OrchestratorConfig,
    )
    from optomind_research.runtime.section_authoring_tool_registry import (
        _make_load_authoring_context,
    )

    blueprint = json.loads(blueprint_path.read_text(encoding="utf-8"))
    section = blueprint["sections"][0]
    orchestrator = FullReviewOrchestrator(OrchestratorConfig(
        blueprint_path=blueprint_path,
        output_root=tmp_path / "out",
        phase3_artifacts_root=root,
        phase3_handoff_mode="legacy_migration",
    ))
    orchestrator._work_dir = tmp_path / "run"
    work_dir = orchestrator._work_dir / "sections" / section["section_id"]
    ctx = orchestrator._build_section_context(
        section, None, None, blueprint, work_dir
    )
    compact_payload = json.loads(
        (work_dir / "PHASE3_AUTHORING_CONTEXT.json").read_text(encoding="utf-8")
    )
    compact_size = len(json.dumps(compact_payload, ensure_ascii=False, separators=(",", ":")))
    assert compact_size < 45_000
    assert "material_bindings" not in compact_payload
    assert "claim_graph" not in compact_payload
    assert all(set(item).issubset({
        "claim_id", "statement", "strength", "writing_permission", "importance",
        "evidence_type", "claim_kind", "evidence_binding_status", "permission_status",
        "claim_state", "section_fit", "supporting_text_chunk_ids",
        "factual_support_chunk_ids", "contextual_support_chunk_ids", "core_chunk_ids",
        "core_paper_ids", "supporting_paper_ids", "supporting_visual_chunk_ids",
        "missing_evidence_components", "statement_source", "statement_integrity_flags",
        "bounded_evidence_paraphrase", "bounded_evidence_paraphrase_excluded",
        "bounded_evidence_paraphrase_exclusion_flags",
    }) for item in compact_payload["claims"])

    load_result = json.loads(_make_load_authoring_context(ctx)())
    load_size = len(json.dumps(load_result, ensure_ascii=False, separators=(",", ":")))
    assert load_result["status"] == "ok"
    assert load_size < 35_000
    assert (work_dir / "SECTION_AUTHORING_CONTEXT.json").stat().st_size < 45_000
    assert len(load_result["claims"]) == 4
    assert all(item.get("supporting_text_chunk_ids") for item in load_result["claims"])
    assert (work_dir / "PHASE3_AUTHORING_AUDIT.json").exists()


def test_r4_low_overlap_review_synthesis_is_warning_not_blocking(tmp_path: Path):
    """Traceable synthesis must not be forced into local sentence entailment."""
    from optomind_research.runtime.section_authoring_tool_registry import (
        _make_run_citation_audit,
        _make_submit_section_draft,
    )

    ctx = _make_context(tmp_path)
    _make_submit_section_draft(ctx)(
        "Across the reviewed literature, the available record provides a useful "
        "context for organizing the mechanism at the section level [REF:p_direct].",
        "initial synthesis",
    )
    result = json.loads(_make_run_citation_audit(ctx)("[]"))
    assert result["status"] == "ok"
    assert result["blocking_flags"] == 0
    assert result["audit_passed"] is True
    assert all(
        item.get("severity") != "blocking"
        for item in result.get("flags_detail", [])
    )


def test_r4_unsupported_formula_remains_blocking(tmp_path: Path):
    """A precise equation is high-risk even when the source is traceable."""
    from optomind_research.runtime.section_authoring_tool_registry import (
        _make_run_citation_audit,
        _make_submit_section_draft,
    )

    ctx = _make_context(tmp_path)
    _make_submit_section_draft(ctx)(
        r"The transfer function obeys $H(\omega)=1/(1+\omega^2)$ [REF:p_direct]. "
        "The surrounding discussion records the section scope, terminology, operating context, "
        "comparison boundary, methodological background, and unresolved interpretation so that "
        "the durable candidate remains useful for a later human review without adding another "
        "unsupported numerical or experimental conclusion. This record remains "
        "available for later audit and qualification by a human reviewer.",
        "formula test",
    )
    result = json.loads(_make_run_citation_audit(ctx)("[]"))
    assert result["blocking_flags"] >= 1
    assert any(
        item.get("risk_class") == "formula_or_symbol"
        for item in result.get("flags_detail", [])
    )


def test_r4_repeated_blocking_signature_stops_and_keeps_durable_candidate(tmp_path: Path):
    """The same unresolved technical flag cannot consume an open-ended loop."""
    from optomind_research.runtime.section_authoring_tool_registry import (
        _make_run_citation_audit,
        _make_submit_revision,
        _make_submit_section_draft,
    )

    ctx = _make_context(tmp_path)
    _make_submit_section_draft(ctx)(
        r"The transfer function obeys $H(\omega)=1/(1+\omega^2)$ [REF:p_direct]. "
        "The surrounding discussion records the section scope, terminology, operating context, "
        "comparison boundary, methodological background, and unresolved interpretation so that "
        "the durable candidate remains useful for a later human review without adding another "
        "unsupported numerical or experimental conclusion. This record remains "
        "available for later audit and qualification by a human reviewer.",
        "initial",
    )
    _write_json(ctx.work_dir / "SECTION_EVIDENCE_PACKET.json", {"items": []})
    first = json.loads(_make_run_citation_audit(ctx)("[]"))
    first_control = json.loads(
        (ctx.work_dir / "SECTION_REVISION_CONTROL.json").read_text(encoding="utf-8")
    )
    assert first_control["stop_revising"] is False

    _make_submit_revision(ctx)(
        r"The transfer function obeys $H(\omega)=1/(1+\omega^2)$ [REF:p_direct]. "
        "The surrounding discussion records the section scope, terminology, operating context, "
        "comparison boundary, methodological background, and unresolved interpretation so that "
        "the durable candidate remains useful for a later human review without adding another "
        "unsupported numerical or experimental conclusion. This record remains "
        "available for later audit and qualification by a human reviewer.",
        "[]",
        "address the formula",
    )
    second = json.loads(_make_run_citation_audit(ctx)("[]"))
    second_control = json.loads(
        (ctx.work_dir / "SECTION_REVISION_CONTROL.json").read_text(encoding="utf-8")
    )
    assert second_control["stop_revising"] is True
    assert second_control["stop_reason"] == "repeated_blocking_signature"

    stopped = _make_submit_revision(ctx)(
        r"The transfer function obeys $H(\omega)=1/(1+\omega^2)$ [REF:p_direct]. "
        "The surrounding discussion records the section scope, terminology, operating context, "
        "comparison boundary, methodological background, and unresolved interpretation so that "
        "the durable candidate remains useful for a later human review without adding another "
        "unsupported numerical or experimental conclusion. This record remains "
        "available for later audit and qualification by a human reviewer.",
        "[]",
        "attempt another paraphrase",
    )
    assert "VALIDATION_PASSED_WITH_LIMITS" in stopped
    package = json.loads(
        (ctx.work_dir / "SECTION_AUTHORING_PACKAGE.json").read_text(encoding="utf-8")
    )
    assert package["authoring_status"] == "completed_with_limits"
    assert package["review_gate"]["blocking_flags"] > 0


def test_r4_real_production_failure_is_a_durable_regression_fixture():
    """The exact expensive run is treated as a convergence regression, offline."""
    root = Path(__file__).resolve().parents[1] / "outputs" / "r4_real_s01_production_budget_20260802"
    section = root / "sections" / "S01"
    if not section.exists():
        pytest.skip("production-budget R4 fixture is not available")
    result = json.loads((section / "RESULT.json").read_text(encoding="utf-8"))
    citation_map = json.loads((section / "SECTION_CITATION_MAP.json").read_text(encoding="utf-8"))
    draft = (section / "SECTION_DRAFT_EN.md").read_text(encoding="utf-8")
    from optomind_research.runtime.section_authoring_tool_registry import (
        _has_durable_section_candidate,
    )

    assert result["status"] == "budget_exhausted"
    assert int(result["total_input_tokens"]) > 800_000
    assert len(citation_map.get("citations", [])) >= 17
    assert len(draft.split()) >= 600
    assert _has_durable_section_candidate(section)


def test_r4_global_semantic_failure_caps_status_at_human_review():
    from optomind_research.runtime.full_review_orchestrator import FullReviewOrchestrator

    orchestrator = FullReviewOrchestrator.__new__(FullReviewOrchestrator)
    orchestrator._section_registry = {
        "sections": [{"section_id": "S01", "status": "completed"}]
    }
    orchestrator._state = {"layer2_audit_failed": True}
    assert orchestrator._determine_final_status({"blocking_flags": 0}) == "awaiting_human_review"


def test_r4_evidence_packet_downshifts_open_permission(tmp_path: Path):
    from optomind_research.runtime.section_authoring_tool_registry import (
        _make_build_evidence_packet,
        _make_load_authoring_context,
    )

    ctx = _make_context(tmp_path)
    _make_load_authoring_context(ctx)()
    result = json.loads(_make_build_evidence_packet(ctx)(json.dumps({
        "items": [{
            "chunk_id": "c_direct",
            "paper_id": "p_direct",
            "claim_ids": ["COPEN"],
            "writing_permission": "factual_assertion",
            "support_hint": "bounded mechanism",
        }],
    })))
    assert result["status"] == "ok"
    assert result["permission_corrections"][0]["normalized_permission"] == "interpretive_synthesis"
    packet = json.loads(
        (ctx.work_dir / "SECTION_EVIDENCE_PACKET.json").read_text(encoding="utf-8")
    )
    assert packet["items"][0]["writing_permission"] == "interpretive_synthesis"


def _make_reconciliation_orchestrator(tmp_path: Path):
    from optomind_research.runtime.full_review_orchestrator import (
        FullReviewOrchestrator,
        OrchestratorConfig,
    )

    blueprint = _make_blueprint(tmp_path)
    run_dir = tmp_path / "r4_run"
    run_dir.mkdir(parents=True)
    orchestrator = FullReviewOrchestrator(
        OrchestratorConfig(blueprint_path=blueprint, output_root=tmp_path / "out")
    )
    orchestrator._run_id = "r4_test"
    orchestrator._work_dir = run_dir
    return orchestrator, run_dir


def _write_durable_candidate(section_dir: Path, *, total_flags: int = 0, blocking_flags: int = 0):
    section_dir.mkdir(parents=True, exist_ok=True)
    paragraph = (
        "This durable candidate explains the mechanism, its operating boundary, "
        "the relationship among the relevant regimes, and the remaining uncertainty. "
        "It is deliberately long enough to represent a usable section draft for "
        "human review and preserves the source-linked evidence package on disk."
    )
    (section_dir / "SECTION_DRAFT_EN.md").write_text(
        paragraph + " " + paragraph,
        encoding="utf-8",
    )
    _write_json(section_dir / "SECTION_EVIDENCE_PACKET.json", {"items": []})
    _write_json(section_dir / "SECTION_CITATION_MAP.json", {"citations": []})
    _write_json(section_dir / "SECTION_AUTHORING_AUDIT.json", {
        "schema_version": "3.0",
        "total_flags": total_flags,
        "total_blocking_flags": blocking_flags,
        "overclaim_flags": [],
        "citation_flags": [],
        "scope_flags": [],
    })
    _write_json(section_dir / "SECTION_AUTHORING_PACKAGE.json", {
        "schema_version": "3.0",
        "section_id": section_dir.name,
        "authoring_status": "awaiting_human_review",
        "total_flags": total_flags,
        "blocking_flags": blocking_flags,
    })


def test_r4_merge_includes_awaiting_candidate_and_excludes_failed(tmp_path: Path):
    orchestrator, run_dir = _make_reconciliation_orchestrator(tmp_path)
    completed_dir = run_dir / "sections" / "S01"
    awaiting_dir = run_dir / "sections" / "S02"
    failed_dir = run_dir / "sections" / "S03"
    for section_dir in (completed_dir, awaiting_dir, failed_dir):
        _write_durable_candidate(section_dir)
    orchestrator._section_registry = {"sections": [
        {"section_id": "S01", "title": "Completed", "status": "completed", "work_dir": str(completed_dir)},
        {"section_id": "S02", "title": "Candidate", "status": "awaiting_human_review", "work_dir": str(awaiting_dir)},
        {"section_id": "S03", "title": "Failed", "status": "failed", "work_dir": str(failed_dir)},
    ]}

    orchestrator._merge_drafts()
    merged = (run_dir / "FULL_REVIEW_DRAFT_EN.md").read_text(encoding="utf-8")
    assert "## Completed" in merged
    assert "## Candidate" in merged
    assert "## Failed" not in merged
    assert {item["section_id"] for item in orchestrator._merged_section_metadata} == {"S01", "S02"}
    assert orchestrator._merged_section_metadata[1]["candidate"] is True
    assert any(item["section_id"] == "S03" for item in orchestrator._excluded_section_metadata)


def test_r4_final_package_counts_section_flags_and_caps_status(tmp_path: Path):
    orchestrator, run_dir = _make_reconciliation_orchestrator(tmp_path)
    section_dir = run_dir / "sections" / "S01"
    _write_durable_candidate(section_dir, total_flags=8, blocking_flags=3)
    _write_json(section_dir / "SECTION_AUTHORING_AUDIT.json", {
        "schema_version": "3.0",
        "total_flags": 8,
        "total_blocking_flags": 3,
        "overclaim_flags": [
            {"flag_type": "overclaim", "severity": "blocking", "reason": "technical item"},
            {"flag_type": "overclaim", "severity": "important", "reason": "editorial item"},
        ],
        "citation_flags": [{"flag_type": "uncited_high_risk_claim", "severity": "blocking", "reason": "missing source"}],
        "scope_flags": [],
    })
    orchestrator._section_registry = {"sections": [{
        "section_id": "S01", "title": "Candidate", "status": "awaiting_human_review", "work_dir": str(section_dir),
    }]}
    orchestrator._merge_drafts()
    final_audit = orchestrator._reconcile_final_audit({"flags": [], "total_flags": 0, "blocking_flags": 0})
    assert final_audit["section_flags_total"] == 8
    assert final_audit["section_blocking_flags"] == 3
    assert final_audit["total_flags"] >= 8
    assert final_audit["blocking_flags"] >= 3
    assert orchestrator._determine_final_status(final_audit) == "awaiting_human_review"


def test_r4_phase3_feedback_blueprint_schema_is_supported(tmp_path: Path):
    from optomind_research.runtime.full_review_orchestrator import (
        FullReviewOrchestrator,
        OrchestratorConfig,
    )

    blueprint = tmp_path / "feedback_blueprint.json"
    _write_json(blueprint, {
        "schema_version": "research_harness.phase2_phase3_s01_feedback_blueprint.v1",
        "sections": [],
    })
    orchestrator = FullReviewOrchestrator(
        OrchestratorConfig(blueprint_path=blueprint, output_root=tmp_path / "out")
    )
    assert orchestrator._load_blueprint()["schema_version"].startswith(
        "research_harness.phase2_phase3_s01_feedback_blueprint.v1"
    )


def test_r4_real_convergence_shape_has_awaiting_durable_candidate():
    root = Path(__file__).resolve().parents[1] / "outputs" / "r4_real_s01_converged_20260802"
    if not root.exists():
        pytest.skip("real convergence fixture is not available")
    registry = json.loads((root / "SECTION_REGISTRY.json").read_text(encoding="utf-8"))
    section = registry["sections"][0]
    work_dir = Path(section["work_dir"])
    package = json.loads((work_dir / "SECTION_AUTHORING_PACKAGE.json").read_text(encoding="utf-8"))
    assert section["status"] == "awaiting_human_review"
    assert package["authoring_status"] == "awaiting_human_review"
    assert package["total_flags"] >= package["blocking_flags"] >= 1
    assert (work_dir / "SECTION_DRAFT_EN.md").stat().st_size > 0


def _make_canonical_wide_ledger_root(tmp_path: Path) -> Path:
    """Canonical handoff with a wide section-scoped ledger and a narrow overlay.

    The canonical coverage_snapshot ledger carries 12 papers and 24 chunks with
    permissions; the legacy overlay on disk only names two papers, which is the
    shape that previously collapsed the author portfolio to a single chunk.
    """

    from optomind_research.runtime.r3_production_handoff import (
        build_r3_production_handoff,
        write_r3_production_handoff,
    )

    root = tmp_path / "phase3"
    topic = {"topic_id": "neutral-mechanism-topic"}
    paper_ids = [f"P{index:02d}" for index in range(1, 13)]
    chunk_ids = [f"K{index:03d}" for index in range(1, 25)]
    claims: list[dict] = []
    binding_claims: dict[str, dict] = {}
    for index in range(1, 33):
        claim_id = f"S01:C{index:02d}"
        start = ((index - 1) * 2) % 24
        support = [chunk_ids[start], chunk_ids[(start + 1) % 24]]
        owners = [paper_ids[(int(item[1:]) - 1) % 12] for item in support]
        claims.append({
            "claim_id": claim_id, "section_id": "S01",
            "statement": (
                f"The bounded mechanism controls the measured response for "
                f"regime {index} under the tested conditions."
            ),
            "criticality": "supporting", "claim_state": "grounded",
            "evidence_type": "mechanism", "support_classification": "supported",
            "evidence_binding_status": "bound", "permission_status": "bound",
            "supporting_chunk_ids": support, "factual_support_chunk_ids": support,
            "core_chunk_ids": support, "core_paper_ids": owners,
        })
        binding_claims[claim_id] = {
            "claim_id": claim_id,
            "evidence_binding_status": "bound", "permission_status": "bound",
            "write_status": "bound",
            "supporting_chunk_ids": support, "factual_support_chunk_ids": support,
            "paper_ids": owners, "support_classification": "supported",
        }
    gap_claim = {
        "claim_id": "S01:GAP", "section_id": "S01",
        "statement": (
            "Boettcher, Real spectra in non-Hermitian Hamiltonians, "
            "Phys. Rev. Lett. 120, 011 (2018)."
        ),
        "criticality": "optional", "claim_state": "open_question",
        "evidence_type": "mechanism", "support_classification": "open_question",
        "evidence_binding_status": "unbound", "permission_status": "unbound",
        "supporting_chunk_ids": [],
    }
    claims.append(gap_claim)
    binding_claims["S01:GAP"] = {
        "claim_id": "S01:GAP",
        "evidence_binding_status": "unbound", "permission_status": "unbound",
        "write_status": "write_with_declared_gap",
        "supporting_chunk_ids": [], "paper_ids": [],
        "support_classification": "open_question",
    }
    inventory = {
        "papers": {
            paper_id: {
                "paper_id": paper_id,
                "title": f"Mechanism study {paper_id}",
                "scope_fit": "direct",
                "use_permission": "factual_support",
                "content_depth": "fulltext",
                "context_complete": True,
            }
            for paper_id in paper_ids
        },
        "chunks": {
            chunk_id: {
                "chunk_id": chunk_id,
                "paper_id": paper_ids[(int(chunk_id[1:]) - 1) % 12],
                "scope_fit": "direct",
                "use_permission": "factual_support",
                "content_depth": "fulltext",
                "context_complete": True,
                "source_kind": "fulltext",
                "normalized_text": (
                    "The bounded mechanism controls the measured response for "
                    f"regime {int(chunk_id[1:]) % 32 + 1} under the tested "
                    "conditions. The measurement record documents the mechanism "
                    "boundary and remaining uncertainty."
                ),
            }
            for chunk_id in chunk_ids
        },
        "visuals": {},
    }
    handoff = build_r3_production_handoff(
        topic_identity=topic,
        sections=[{"section_id": "S01", "title": "Bounded mechanism", "topic_identity": dict(topic)}],
        coverage_atlas={
            "schema_version": "research_harness.coverage_atlas.v1",
            "topic_identity": dict(topic),
            "sections": [{"section_id": "S01", "needs_expansion": True}],
            "relation_graph": {"edge_count": 0},
        },
        section_argument_contracts={"S01": {
            "schema_version": "research_harness.section_argument_contract.v1",
            "section_id": "S01", "status": "contract_ready",
            "argument_tasks": [{"task_id": "S01:T01", "description": "Explain the bounded mechanism."}],
        }},
        claims_by_criticality={"load_bearing": [], "supporting": claims[:32], "optional": [gap_claim]},
        material_inventory=inventory,
        material_bindings={"S01": {"section_id": "S01", "claims": binding_claims}},
        relation_graph={"schema_version": "r3.relation_graph.v1", "edges": []},
        claim_dag={"schema_version": "research_harness.claim_graph.v1", "edges": []},
        gaps=[], coverage_requests=[],
        synthesis_bundles={"S01": {
            "section_id": "S01", "status": "needs_more_literature",
            "section_outcome": "needs_more_literature",
            "readiness_status": "needs_more_literature",
            "paper_ids": paper_ids, "chunk_ids": chunk_ids,
            "claim_category_assignments": [
                {"claim_id": claim_id, "category": "open_questions" if claim_id == "S01:GAP" else "established_points"}
                for claim_id in binding_claims
            ],
        }},
        visual_bindings={"S01": []}, visual_needs={"S01": []},
    )
    write_r3_production_handoff(
        root / "R3_PRODUCTION_HANDOFF.json",
        handoff,
        fail_on_invalid=True,
    )

    # Wide canonical ledger (the source of record) + narrow legacy overlay.
    _write_json(
        root / "coverage_snapshot" / "sections" / "S01" / "SECTION_SOURCE_LEDGER.json",
        {
            "section_id": "S01",
            "sources": [
                {
                    "paper_id": paper_id, "title": f"Mechanism study {paper_id}",
                    "literature_role": "mechanism", "scope_fit": "direct",
                    "canonical_chunk_ids": [chunk_ids[index * 2], chunk_ids[index * 2 + 1]],
                    "acquisition_status": "fulltext", "content_depth": "fulltext",
                    "context_complete": True, "use_permission": "factual_support",
                    "discovery_route": "phase3_test", "materialization_route": "oa_pdf",
                }
                for index, paper_id in enumerate(paper_ids)
            ],
        },
    )
    _write_json(
        root / "coverage_requests" / "S01" / "SECTION_ASSET_OVERLAY.json",
        {"section_id": "S01", "paper_ids": ["P01", "P02"]},
    )
    kb_path = root / "shared_kb.sqlite"
    conn = sqlite3.connect(str(kb_path))
    try:
        conn.execute(
            "CREATE TABLE text_chunks ("
            "chunk_id TEXT, paper_id TEXT, title TEXT, text TEXT, "
            "evidence_level TEXT, source_kind TEXT, discovery_route TEXT, "
            "content_depth TEXT, context_complete INTEGER, use_permission TEXT, "
            "route_provenance_json TEXT, scope_fit TEXT)"
        )
        provenance = json.dumps({
            "migration": "r3_2",
            "use_permission": "factual_support",
        })
        conn.executemany(
            "INSERT INTO text_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    chunk_id,
                    paper_ids[(int(chunk_id[1:]) - 1) % 12],
                    f"Mechanism study {paper_ids[(int(chunk_id[1:]) - 1) % 12]}",
                    inventory["chunks"][chunk_id]["normalized_text"],
                    "fulltext",
                    "fulltext",
                    "phase3_test",
                    "fulltext",
                    1,
                    "factual_support",
                    provenance,
                    "direct",
                )
                for chunk_id in chunk_ids
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return root


def test_r4_canonical_wide_ledger_drops_overlay_and_bounds_core(
    tmp_path: Path,
) -> None:
    from optomind_research.runtime.full_review_orchestrator import (
        FullReviewOrchestrator,
        OrchestratorConfig,
    )
    from optomind_research.runtime.r4_phase3_artifacts import R4Phase3ArtifactStore
    from optomind_research.runtime.section_authoring_tool_registry import (
        _build_asset_graph,
        _build_authoring_evidence_portfolio,
        _make_load_authoring_context,
    )

    root = _make_canonical_wide_ledger_root(tmp_path)
    artifacts = R4Phase3ArtifactStore(root).section("S01")
    assert artifacts.production_handoff_valid is True
    assert artifacts.source_ledger_path is not None
    assert "coverage_snapshot" in str(artifacts.source_ledger_path)
    assert artifacts.overlay_path is None

    orchestrator = FullReviewOrchestrator(OrchestratorConfig(
        blueprint_path=_make_blueprint(tmp_path),
        output_root=tmp_path / "out",
        phase3_artifacts_root=root,
        phase3_handoff_mode="canonical",
    ))
    orchestrator._work_dir = tmp_path / "run"
    orchestrator._work_dir.mkdir(parents=True, exist_ok=True)
    section = {"section_id": "S01", "title": "A bounded mechanism", "argument_role": "mechanism"}
    ctx = orchestrator._build_section_context(
        section, None, None, {"sections": [section]},
        tmp_path / "run" / "sections" / "S01",
    )
    assert ctx.section_data["phase3_artifacts"]["excluded_claim_ids"] == ["S01:GAP"]

    payload = json.loads(_make_load_authoring_context(ctx)())
    claim_ids = [item["claim_id"] for item in payload["claims"]]
    assert len(claim_ids) == 32
    assert "S01:GAP" not in claim_ids
    persisted = json.loads(
        (ctx.work_dir / "SECTION_AUTHORING_CONTEXT.json").read_text(encoding="utf-8")
    )
    assert [item["claim_id"] for item in persisted["claims"]] == claim_ids

    graph = _build_asset_graph(ctx)
    assert len(graph.papers) >= 12
    assert len(graph.chunks) >= 24
    portfolio = _build_authoring_evidence_portfolio(ctx, graph)
    recommended = portfolio["recommended_batch_chunk_ids"]
    assert len(recommended) >= 4
    assert len(recommended) <= 12
    assert portfolio["selected_paper_count"] >= 4


def test_r4_legacy_migration_retains_overlay_and_narrow_portfolio(
    tmp_path: Path,
) -> None:
    from optomind_research.runtime.section_authoring_tool_registry import (
        _build_asset_graph,
        _build_authoring_evidence_portfolio,
    )

    ctx = _make_context(tmp_path)
    assert ctx.section_overlay_path is not None
    portfolio = _build_authoring_evidence_portfolio(ctx, _build_asset_graph(ctx))
    assert portfolio["selected_paper_count"] <= 2


def test_compact_authoring_task_metadata_sets_per_task_tool_result_allowance(
    tmp_path: Path,
) -> None:
    from optomind_research.runtime.full_review_orchestrator import (
        OrchestratorConfig,
        _compact_authoring_task_metadata,
    )

    config = OrchestratorConfig(
        blueprint_path=_make_blueprint(tmp_path),
        output_root=tmp_path / "out",
        compact_tool_result_limit=40_000,
        compact_workspace_target_tokens=22_000,
    )
    metadata = _compact_authoring_task_metadata(config)
    assert metadata == {
        "context_tool_result_limit": 40_000,
        "compact_workspace_target_tokens": 22_000,
    }


def test_build_section_context_exposes_compact_adaptive_config(
    tmp_path: Path,
) -> None:
    from optomind_research.runtime.full_review_orchestrator import (
        FullReviewOrchestrator,
        OrchestratorConfig,
    )

    root = _make_canonical_wide_ledger_root(tmp_path)
    orchestrator = FullReviewOrchestrator(OrchestratorConfig(
        blueprint_path=_make_blueprint(tmp_path),
        output_root=tmp_path / "out",
        phase3_artifacts_root=root,
        phase3_handoff_mode="canonical",
        compact_tool_result_limit=40_000,
        compact_workspace_target_tokens=22_000,
        authoring_core_chunk_min=10,
        authoring_core_chunk_max=16,
    ))
    orchestrator._work_dir = tmp_path / "run"
    orchestrator._work_dir.mkdir(parents=True, exist_ok=True)
    section = {"section_id": "S01", "title": "A bounded mechanism", "argument_role": "mechanism"}
    ctx = orchestrator._build_section_context(
        section, None, None, {"sections": [section]},
        tmp_path / "run" / "sections" / "S01",
    )
    assert ctx.section_data["authoring_core_chunk_limit"] == 12
    assert ctx.section_data["authoring_core_chunk_min"] == 10
    assert ctx.section_data["authoring_core_chunk_max"] == 16
    assert ctx.section_data["compact_tool_result_limit"] == 40_000
    assert ctx.section_data["compact_workspace_target_tokens"] == 22_000


def test_compact_workspace_diagnostics_use_per_task_allowance(
    tmp_path: Path,
) -> None:
    from optomind_research.runtime.compact_section_authoring import (
        CompactSectionAuthoringToolProvider,
        _estimate_workspace_tokens,
    )
    from optomind_research.runtime.full_review_orchestrator import (
        FullReviewOrchestrator,
        OrchestratorConfig,
    )

    root = _make_canonical_wide_ledger_root(tmp_path)
    orchestrator = FullReviewOrchestrator(OrchestratorConfig(
        blueprint_path=_make_blueprint(tmp_path),
        output_root=tmp_path / "out",
        phase3_artifacts_root=root,
        phase3_handoff_mode="canonical",
        compact_tool_result_limit=40_000,
    ))
    orchestrator._work_dir = tmp_path / "run"
    orchestrator._work_dir.mkdir(parents=True, exist_ok=True)
    section = {"section_id": "S01", "title": "A bounded mechanism", "argument_role": "mechanism"}
    ctx = orchestrator._build_section_context(
        section, None, None, {"sections": [section]},
        tmp_path / "run" / "sections" / "S01",
    )
    provider = CompactSectionAuthoringToolProvider(ctx)
    functions = {
        tool.name: tool._func for tool in provider.get_tools(ctx.work_dir)
    }
    payload = json.loads(functions["prepare_authoring_workspace"]())
    diagnostics = payload["workspace_diagnostics"]
    assert diagnostics["claim_count"] == 32
    assert diagnostics["selected_chunk_count"] == 12
    assert diagnostics["claim_coverage"] == 32
    assert diagnostics["tool_result_allowance_tokens"] == 40_000
    assert diagnostics["truncation_risk"] is False
    assert _estimate_workspace_tokens(payload) <= 40_000
    assert payload["protocol"] == "compact_section_authoring.v2"
