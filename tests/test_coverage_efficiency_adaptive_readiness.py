"""Offline adversarial checks for bounded coverage and adaptive readiness."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_contract(filename: str, module_name: str):
    path = ROOT / "optomind_research" / "runtime" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


COVERAGE = _load_contract(
    "coverage_decision_contract.py",
    "coverage_efficiency_contract_under_test",
)
QUALITY = _load_contract(
    "review_quality_contract.py",
    "coverage_efficiency_quality_under_test",
)


def _source(
    paper_id: str,
    role: str,
    *,
    permission: str = "factual_support",
    depth: str = "structured_snippet",
    venue: str = "Optics Letters",
    claims: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "paper_id": paper_id,
        "title": f"{role} evidence {paper_id}",
        "venue": venue,
        "literature_role": role,
        "scope_fit": "direct",
        "content_depth": depth,
        "use_permission": permission,
        "context_complete": depth in {"structured_snippet", "fulltext"},
        "canonical_chunk_ids": [f"s2chunk:{paper_id}:0"],
        "supported_claim_ids": list(claims or []),
    }


def test_compact_audit_is_delta_only_and_one_per_wave() -> None:
    section = {
        "section_id": "S-EFF",
        "title": "Adaptive metasurface mechanism",
        "topic_identity": {"scientific_object": "adaptive metasurface mechanism"},
    }
    candidates = [
        {
            "candidate_id": f"candidate-{index}",
            "doi": f"10.1234/example.{index}",
            "title": f"Candidate {index}",
            "abstract": "long abstract text " * 500,
            "role": "mechanism",
        }
        for index in range(20)
    ]
    payload = COVERAGE.build_compact_batched_audit_payload(
        section=section,
        candidates=candidates,
        wave_index=0,
        max_candidates=6,
        components=["phase matching", "coupling regime"],
    )
    assert len(payload["candidates"]) == 6
    assert all(len(row["abstract"]) <= 420 for row in payload["candidates"])
    assert payload == COVERAGE.build_compact_batched_audit_payload(
        section=section,
        candidates=candidates,
        wave_index=0,
        max_candidates=6,
        components=["phase matching", "coupling regime"],
    )
    assert payload["estimated_input_tokens"] < 2_000

    admitted = COVERAGE.admit_batched_audit_call(
        wave_index=0,
        audit_calls_in_wave=0,
        predicted_input_tokens=payload["estimated_input_tokens"],
        output_reserve_tokens=200,
        cumulative_input_tokens=0,
        cumulative_budget_tokens=3_000,
        per_call_budget_tokens=2_000,
        audit_calls_total=0,
        audit_call_budget=2,
    )
    assert admitted.admitted is True
    repeated_wave = COVERAGE.admit_batched_audit_call(
        wave_index=0,
        audit_calls_in_wave=1,
        predicted_input_tokens=100,
        output_reserve_tokens=100,
        cumulative_input_tokens=0,
        cumulative_budget_tokens=3_000,
        per_call_budget_tokens=2_000,
        audit_calls_total=1,
        audit_call_budget=2,
    )
    assert repeated_wave.admitted is False
    assert repeated_wave.reason == "one_batched_audit_per_wave_exceeded"


def test_adaptive_contract_is_role_and_risk_specific_not_a_lowered_universal_gate() -> None:
    introduction = {
        "section_id": "intro",
        "section_role": "introduction",
        "target_word_count": 900,
        "load_bearing_claims": [{"claim_id": "intro-c1", "statement": "basis"}],
    }
    intro_contract = QUALITY.build_adaptive_coverage_contract(introduction)
    assert intro_contract.required_roles == ["foundation"]
    assert "method" not in intro_contract.required_roles

    intro_ready = QUALITY.evaluate_adaptive_coverage(
        introduction,
        [
            _source("p1", "foundation", claims=["intro-c1"]),
            _source("p2", "foundation", venue="Nano Letters", claims=["intro-c1"]),
        ],
        legacy_targets={"minimum_unique_sources": 11, "minimum_direct_sources": 7},
    )
    assert intro_ready.outcome == "material_ready_with_limits"
    assert any("article_breadth_target_shortfall" in item for item in intro_ready.limitations)

    outlook = {
        "section_id": "outlook",
        "section_role": "outlook",
        "target_word_count": 700,
        "load_bearing_claims": [{"claim_id": "out-c1", "statement": "frontier"}],
    }
    outlook_contract = QUALITY.build_adaptive_coverage_contract(outlook)
    assert outlook_contract.required_roles == ["frontier"]
    assert "foundation" not in outlook_contract.required_roles
    outlook_ready = QUALITY.evaluate_adaptive_coverage(
        outlook,
        [_source("p3", "frontier", claims=["out-c1"])],
    )
    assert outlook_ready.outcome == "material_ready_with_limits"

    high_risk = {
        "section_id": "mechanism",
        "section_role": "mechanism",
        "target_word_count": 2400,
        "risk_level": "high",
        "load_bearing_claims": [{"claim_id": "m-c1", "statement": "causal claim"}],
    }
    permission_failed = QUALITY.evaluate_adaptive_coverage(
        high_risk,
        [_source("p4", "mechanism", permission="discovery_only", claims=["m-c1"])],
    )
    assert permission_failed.outcome == "needs_more_literature"
    assert permission_failed.permission_failures
    assert permission_failed.factual_permission_sources == 0

    mergeable = QUALITY.evaluate_adaptive_coverage(
        {
            "section_id": "thin",
            "section_role": "application",
            "merge_if_under_supported": True,
        },
        [],
    )
    assert mergeable.outcome == "merge_required"


def test_substantive_application_section_cannot_author_from_one_paper() -> None:
    source = _source("p1", "application", claims=[])
    source["literature_roles"] = ["application", "frontier"]
    readiness = QUALITY.evaluate_adaptive_coverage(
        {
            "section_id": "application",
            "section_role": "application",
            "target_word_count": 1200,
            "required_roles": ["application", "frontier"],
            "load_bearing_claims": [],
        },
        [source],
    )

    assert readiness.outcome == "needs_more_literature"
    assert any(
        item.startswith("substantive_section_requires_plural_sources:1/2")
        for item in readiness.reasons
    )


def test_s2_snippet_is_peer_evidence_but_visual_or_shallow_text_escalates() -> None:
    accepted = COVERAGE.structured_snippet_route_decision(
        text="short but structured body text",
        scope_fit="direct",
        context_complete=True,
        use_permission="factual_support",
    )
    assert accepted["accepted_as_peer_text_evidence"] is True
    assert accepted["fulltext_escalation_required"] is False

    shallow = COVERAGE.structured_snippet_route_decision(
        text="metadata-like fragment",
        scope_fit="direct",
        context_complete=False,
        use_permission="factual_support",
    )
    assert shallow["accepted_as_peer_text_evidence"] is False
    assert shallow["fulltext_escalation_required"] is True

    visual = COVERAGE.structured_snippet_route_decision(
        text="complete body passage",
        scope_fit="direct",
        context_complete=True,
        use_permission="factual_support",
        visual_required=True,
    )
    assert visual["accepted_as_peer_text_evidence"] is False
    assert "visual_asset_required" in visual["reason"]


def test_role_specific_queries_are_stable_and_not_generic_breadth_retries() -> None:
    section = {
        "section_id": "S-QUERY",
        "title": "Adaptive metasurface devices",
        "topic_identity": {
            "scientific_object": "adaptive metasurface devices",
            "core_anchor_tokens": ["adaptive", "metasurface", "devices"],
        },
    }
    mechanism = COVERAGE.build_uncovered_query_targets(
        section,
        roles=["mechanism"],
        components=["coupling regime"],
        max_targets=4,
    )
    application = COVERAGE.build_uncovered_query_targets(
        section,
        roles=["application"],
        components=["coupling regime"],
        max_targets=4,
    )
    assert mechanism != application
    assert any("causal mechanism" in row["query"] for row in mechanism)
    assert any("deployment" in row["query"] or "application" in row["query"] for row in application)


def test_runtime_query_targets_respect_adaptive_intro_roles(tmp_path: Path) -> None:
    try:
        import optomind_research.runtime.section_coverage_tool_registry as registry
        from optomind_research.runtime.tool_provider import SectionCoverageContext
    except ModuleNotFoundError as exc:
        pytest.skip(f"registry dependency unavailable: {exc}")

    ctx = SectionCoverageContext(
        section_id="S-INTRO",
        section_data={
            "section_id": "S-INTRO",
            "title": "Introduction to adaptive metasurfaces",
            "section_role": "introduction",
            "required_roles": ["foundation", "mechanism", "method", "frontier"],
            "adaptive_coverage_enabled": True,
        },
        kb_sqlite=None,
        temp_kb_sqlite=tmp_path / "stage.sqlite",
        work_dir=tmp_path,
    )
    ctx.adaptive_coverage_enabled = True
    targets = registry._coverage_query_targets(ctx)
    assert targets
    assert {row.get("role") for row in targets} == {"foundation"}


def test_article_portfolio_deduplicates_and_reuses_audit_and_material(tmp_path: Path) -> None:
    try:
        import optomind_research.runtime.section_coverage_tool_registry as registry
    except ModuleNotFoundError as exc:
        pytest.skip(f"registry dependency unavailable: {exc}")

    portfolio_path = tmp_path / "ARTICLE_EVIDENCE_PORTFOLIO.json"
    ctx = SimpleNamespace(
        section_id="S-ONE",
        section_data={"topic_identity": {"fingerprint": "topic-1"}},
        work_dir=tmp_path,
        article_evidence_portfolio_path=portfolio_path,
    )
    first = registry._upsert_article_candidate(
        ctx,
        {
            "candidate_id": "local-one",
            "role": "foundation",
            "doi": "10.1234/shared",
            "title": "Shared candidate",
            "abstract": "one abstract",
        },
    )
    second = registry._upsert_article_candidate(
        ctx,
        {
            "candidate_id": "local-two",
            "role": "mechanism",
            "doi": "10.1234/shared",
            "title": "Shared candidate, richer title",
            "abstract": "one abstract with more detail",
        },
    )
    assert first["candidate_id"] == second["candidate_id"]
    portfolio = json.loads(portfolio_path.read_text(encoding="utf-8"))
    assert len(portfolio["candidates"]) == 1
    assert portfolio["telemetry"]["duplicate_identities_collapsed"] == 1

    registry._record_article_audit(
        ctx,
        {
            **second,
            "decision": "approved",
            "scope_fit": "direct",
            "role_fit": ["foundation", "mechanism"],
            "audit_reason": "shared direct evidence",
        },
    )
    reused = registry._upsert_article_candidate(
        ctx,
        {"role": "mechanism", "doi": "10.1234/shared", "title": "reloaded"},
    )
    assert reused["decision"] == "approved"
    registry._record_article_material(
        ctx,
        reused,
        paper_id="paper-shared",
        chunk_ids=["chunk-shared"],
    )
    material = registry._article_material_for_candidate(ctx, reused)
    assert material["chunk_ids"] == ["chunk-shared"]
    final = json.loads(portfolio_path.read_text(encoding="utf-8"))
    assert final["telemetry"]["audit_reuse_hits"] >= 1
    assert final["telemetry"]["material_reuse_hits"] >= 1


def test_adaptive_validator_admits_qualified_intro_without_11_7_block(tmp_path: Path) -> None:
    try:
        import optomind_research.runtime.section_coverage_tool_registry as registry
        from optomind_research.runtime.tool_provider import SectionCoverageContext
    except ModuleNotFoundError as exc:
        pytest.skip(f"registry dependency unavailable: {exc}")

    section = {
        "section_id": "S-INTRO",
        "title": "Introduction to adaptive metasurfaces",
        "section_role": "introduction",
        "target_word_count": 900,
        "required_roles": ["foundation", "mechanism", "method", "frontier"],
        "topic_identity": {"valid": False},
        "adaptive_coverage_enabled": True,
    }
    ctx = SectionCoverageContext(
        section_id="S-INTRO",
        section_data=section,
        kb_sqlite=None,
        temp_kb_sqlite=tmp_path / "stage.sqlite",
        work_dir=tmp_path,
    )
    ctx.adaptive_coverage_enabled = True
    (tmp_path / "SECTION_CONTEXT.json").write_text(
        json.dumps({
            "section_id": "S-INTRO",
            "section_title": section["title"],
            "chapter_argument": "Establish the basis before the mechanism.",
            "required_roles": section["required_roles"],
        }),
        encoding="utf-8",
    )
    (tmp_path / "SECTION_COVERAGE_PLAN.json").write_text(
        json.dumps({
            "roles": {
                "foundation": {"priority": "required"},
                "mechanism": {"priority": "useful"},
            }
        }),
        encoding="utf-8",
    )
    (tmp_path / "LOCAL_COVERAGE_AUDIT.json").write_text(
        json.dumps({"blocking_gaps": [], "important_gaps": []}),
        encoding="utf-8",
    )
    (tmp_path / "SECTION_SOURCE_LEDGER.json").write_text(
        json.dumps({
            "sources": [
                _source("intro-p1", "foundation", venue="Optics Letters"),
                _source("intro-p2", "foundation", venue="Nano Letters"),
            ]
        }),
        encoding="utf-8",
    )

    result = registry._make_validate_section_coverage_package(ctx)()
    assert result.startswith("VALIDATION_PASSED")
    package = json.loads(
        (tmp_path / "SECTION_MATERIAL_PACKAGE.json").read_text(encoding="utf-8")
    )
    assert package["coverage_outcome"] == "material_ready_with_limits"
    assert package["breadth_target_met"] is False
    assert package["blocking_gaps_remain"] is False
