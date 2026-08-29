"""Focused offline tests for the v2 structured compact author workspace."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from optomind_research.runtime.compact_section_authoring import (
    CompactSectionAuthoringToolProvider,
    _build_local_evidence_relations,
    _compact_claim_card,
    _estimate_workspace_tokens,
    _resolve_claim_citation_handles,
    _select_adaptive_core_chunks,
    _synthesize_local_candidate,
)
from optomind_research.runtime.section_authoring_tool_registry import (
    _build_asset_graph,
)
from optomind_research.runtime.section_authoring_assets import ChunkAsset
from optomind_research.runtime.tool_provider import SectionAuthoringContext


def _make_wide_ctx(
    tmp_path: Path,
    *,
    paper_count: int = 12,
    chunks_per_paper: int = 2,
    claim_count: int = 32,
    rich: bool = False,
    claim_support: str = "pair",
    min_limit: int = 8,
) -> SectionAuthoringContext:
    """Build a synthetic canonical section (papers + chunks + claims)."""

    paper_ids = [f"P{i:02d}" for i in range(1, paper_count + 1)]
    chunk_ids = [
        f"K{i:03d}"
        for i in range(1, paper_count * chunks_per_paper + 1)
    ]
    sources = []
    for index, paper_id in enumerate(paper_ids):
        sources.append({
            "paper_id": paper_id,
            "title": f"Mechanism study {paper_id}",
            "literature_role": "mechanism",
            "scope_fit": "direct",
            "canonical_chunk_ids": [
                chunk_ids[index * chunks_per_paper + offset]
                for offset in range(chunks_per_paper)
            ],
            "acquisition_status": "fulltext",
            "content_depth": "fulltext",
            "context_complete": True,
            "use_permission": "factual_support",
            "discovery_route": "phase3_test",
            "materialization_route": "oa_pdf",
        })
    (tmp_path / "SECTION_SOURCE_LEDGER.json").write_text(
        json.dumps({"section_id": "S03", "sources": sources}),
        encoding="utf-8",
    )
    (tmp_path / "SECTION_MATERIAL_PACKAGE.json").write_text(
        json.dumps({
            "coverage_status": "completed",
            "total_sources": paper_count,
            "sources_by_role": {"mechanism": paper_count},
            "blocking_gaps_remain": False,
            "chunk_ids_by_role": {"mechanism": chunk_ids},
        }),
        encoding="utf-8",
    )
    kb_path = tmp_path / "main_kb.sqlite"
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
        rows = []
        for chunk_id in chunk_ids:
            owner = paper_ids[(int(chunk_id[1:]) - 1) % paper_count]
            regime = ((int(chunk_id[1:]) - 1) % 32) + 1
            if rich:
                text = (
                    "The bounded mechanism controls the measured response for "
                    f"regime {regime} under the tested conditions. The measurement "
                    "record documents the mechanism boundary, the material platform, "
                    "the operating wavelength, and the remaining uncertainty for later "
                    "qualification by a human reviewer. "
                    + (
                        "Repetition of the calibrated measurement conditions across "
                        "independent samples confirms the stability of the reported "
                        "response and the reproducibility of the fabrication process. "
                    ) * 32
                )
            else:
                text = (
                    "The bounded mechanism controls the measured response for "
                    f"regime {regime} under the tested conditions. The measurement "
                    "record documents the mechanism boundary and remaining uncertainty."
                )
            rows.append((
                chunk_id, owner, f"Mechanism study {owner}", text,
                "fulltext", "fulltext", "phase3_test", "fulltext", 1,
                "factual_support", provenance, "direct",
            ))
        conn.executemany(
            "INSERT INTO text_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    claims = []
    for index in range(1, claim_count + 1):
        claim_id = f"C{index:02d}"
        if claim_support == "single":
            support = [chunk_ids[(index - 1) % len(chunk_ids)]]
        elif claim_support == "shared":
            support = [chunk_ids[0]]
        elif claim_support == "dup_pair":
            support = [
                chunk_ids[(index - 1) % len(chunk_ids)],
                chunk_ids[
                    ((index - 1) % len(chunk_ids) + paper_count) % len(chunk_ids)
                ],
            ]
        elif claim_support == "quad":
            start = ((index - 1) * 4) % len(chunk_ids)
            support = [
                chunk_ids[(start + offset) % len(chunk_ids)]
                for offset in range(4)
            ]
        elif claim_support == "pair_12":
            start = ((index - 1) * 2) % 12
            support = [
                chunk_ids[start],
                chunk_ids[(start + 1) % 12],
            ]
        else:
            start = ((index - 1) * 2) % len(chunk_ids)
            support = [
                chunk_ids[start],
                chunk_ids[(start + 1) % len(chunk_ids)],
            ]
        owners = [
            paper_ids[(int(chunk_id[1:]) - 1) % paper_count]
            for chunk_id in support
        ]
        statement = (
            f"The bounded mechanism controls the measured response for "
            f"regime {index} under the tested conditions."
        )
        if rich:
            statement += (
                " The calibrated measurement conditions are stable across "
                "independent samples, and the remaining uncertainty is "
                "explicitly bounded for later human qualification. "
                "The operating wavelength and material platform are recorded, "
                "and the reproducibility of the fabrication process is "
                "confirmed across independent samples. The measured response "
                "remains within the calibrated tolerance band under repeated "
                "excitation, and the fabrication tolerances are documented "
                "for the reported operating regime. The boundary conditions "
                "and comparison with prior literature are recorded so the "
                "qualified statement does not overreach beyond the tested "
                "parameter space."
            )
        claims.append({
            "claim_id": claim_id,
            "statement": statement,
            "strength": "qualified",
            "writing_permission": "hedged_factual_assertion",
            "importance": "load_bearing",
            "evidence_type": "measurement",
            "claim_kind": "direct_fact",
            "supporting_text_chunk_ids": support,
            "supporting_chunk_ids": support,
            "factual_support_chunk_ids": support,
            "core_chunk_ids": support,
            "core_paper_ids": owners,
            "paper_ids": owners,
            "support_classification": "supported",
        })
    return SectionAuthoringContext(
        section_id="S03",
        section_data={
            "section_id": "S03",
            "title": "Fabrication Realization",
            "chapter_argument": (
                "Translate computed diffractive transmission functions into "
                "physical devices with bounded fabrication claims."
            ),
            "claims": claims,
            "authoring_core_chunk_limit": 12,
            "authoring_core_chunk_min": min_limit,
            "authoring_core_chunk_max": 16,
            "compact_workspace_target_tokens": 25_000,
            "compact_tool_result_limit": 32_000,
            "section_contract": {"word_budget": 1400},
            "scope_guardrails": ["Keep claims hedged."],
            "claim_strength_policy": {
                "qualified": "hedged_factual_assertion",
            },
        },
        kb_sqlite=kb_path,
        temp_kb_sqlite=None,
        work_dir=tmp_path,
        source_ledger_path=tmp_path / "SECTION_SOURCE_LEDGER.json",
    )


def _prepare(provider, work_dir: Path) -> dict:
    functions = {
        tool.name: tool._func for tool in provider.get_tools(work_dir)
    }
    return json.loads(functions["prepare_authoring_workspace"]())


def test_workspace_preserves_all_claim_cards_and_removes_duplicate_payloads(
    tmp_path: Path,
) -> None:
    ctx = _make_wide_ctx(tmp_path)
    payload = _prepare(CompactSectionAuthoringToolProvider(ctx), tmp_path)

    cards = payload["claim_cards"]
    assert len(cards) == 32
    assert [card["claim_id"] for card in cards] == [
        f"C{i:02d}" for i in range(1, 33)
    ]
    assert all(card["citation_handle"] for card in cards)
    assert all(card["evidence_chunk_ids"] for card in cards)
    # Duplicated raw payloads are gone from the model-facing workspace.
    assert "claims" not in payload["context"]
    assert "synthesis_bundle" not in payload["context"]
    assert "judgment_ledger" not in payload["context"]
    assert "claims" not in payload["materials"]
    assert "evidence_portfolio" not in payload["materials"]
    assert payload["protocol"] == "compact_section_authoring.v2"


def test_adaptive_selection_default_twelve_is_deterministic(tmp_path: Path) -> None:
    ctx = _make_wide_ctx(
        tmp_path, paper_count=12, chunks_per_paper=1, claim_count=32
    )
    graph = _build_asset_graph(ctx)
    claims = ctx.section_data["claims"]
    first = _select_adaptive_core_chunks(
        claims=claims,
        graph=graph,
        portfolio_recommended=list(graph.chunks),
        core_limit=12,
        min_limit=8,
        max_limit=16,
    )
    second = _select_adaptive_core_chunks(
        claims=claims,
        graph=graph,
        portfolio_recommended=list(graph.chunks),
        core_limit=12,
        min_limit=8,
        max_limit=16,
    )
    assert first["chunk_ids"] == second["chunk_ids"]
    assert len(first["chunk_ids"]) == 12
    assert first["covered_claim_count"] == 32
    assert first["unique_paper_count"] == 12


def test_adaptive_selection_expands_only_for_real_coverage_up_to_max(
    tmp_path: Path,
) -> None:
    ctx = _make_wide_ctx(
        tmp_path,
        paper_count=16,
        chunks_per_paper=2,
        claim_count=32,
        claim_support="single",
    )
    graph = _build_asset_graph(ctx)
    claims = ctx.section_data["claims"]
    selected = _select_adaptive_core_chunks(
        claims=claims,
        graph=graph,
        portfolio_recommended=list(graph.chunks)[:12],
        core_limit=12,
        min_limit=8,
        max_limit=16,
    )
    assert len(selected["chunk_ids"]) == 16
    assert selected["reason"] == "expanded_for_claim_or_paper_coverage"
    assert selected["covered_claim_count"] < selected["total_claim_count"]


def test_adaptive_selection_shrinks_when_coverage_and_diversity_preserved(
    tmp_path: Path,
) -> None:
    ctx = _make_wide_ctx(
        tmp_path,
        paper_count=8,
        chunks_per_paper=2,
        claim_count=8,
        claim_support="dup_pair",
    )
    graph = _build_asset_graph(ctx)
    claims = ctx.section_data["claims"]
    selected = _select_adaptive_core_chunks(
        claims=claims,
        graph=graph,
        portfolio_recommended=list(graph.chunks),
        core_limit=12,
        min_limit=8,
        max_limit=16,
    )
    assert len(selected["chunk_ids"]) == 8
    assert selected["reason"] == "shrunk_redundant_without_coverage_loss"
    assert selected["covered_claim_count"] == 8
    assert selected["unique_paper_count"] == 8


def test_adaptive_selection_available_less_than_min(tmp_path: Path) -> None:
    ctx = _make_wide_ctx(
        tmp_path,
        paper_count=1,
        chunks_per_paper=2,
        claim_count=1,
    )
    graph = _build_asset_graph(ctx)
    selected = _select_adaptive_core_chunks(
        claims=ctx.section_data["claims"],
        graph=graph,
        portfolio_recommended=list(graph.chunks),
        core_limit=12,
        min_limit=8,
        max_limit=16,
    )
    assert len(selected["chunk_ids"]) == 2
    assert selected["reason"] == "available_less_than_min"


def test_workspace_size_target_and_diagnostics_for_rich_section(
    tmp_path: Path,
) -> None:
    ctx = _make_wide_ctx(
        tmp_path,
        paper_count=12,
        chunks_per_paper=1,
        claim_count=32,
        claim_support="quad",
        rich=True,
        min_limit=12,
    )
    payload = _prepare(CompactSectionAuthoringToolProvider(ctx), tmp_path)
    diagnostics = payload["workspace_diagnostics"]
    estimated = _estimate_workspace_tokens(payload)
    assert abs(estimated - diagnostics["estimated_token_size"]) <= 2
    assert 20_000 <= estimated <= 25_000
    assert diagnostics["claim_count"] == 32
    assert diagnostics["selected_chunk_count"] == 12
    assert diagnostics["unique_paper_count"] >= 8
    assert diagnostics["claim_coverage"] == 32
    assert diagnostics["selection_bounds"] == {
        "min": 12,
        "default": 12,
        "max": 16,
        "selected": 12,
        "reason": diagnostics["selection_bounds"]["reason"],
    }
    assert diagnostics["tool_result_allowance_tokens"] == 32_000
    assert diagnostics["truncation_risk"] is False


def test_claim_handle_resolution_local_and_unknown_is_ignored(
    tmp_path: Path,
) -> None:
    ctx = _make_wide_ctx(tmp_path, paper_count=2, chunks_per_paper=2, claim_count=2)
    graph = _build_asset_graph(ctx)
    relations = _build_local_evidence_relations(
        ctx, graph, ctx.section_data["claims"]
    )
    authorable = {"C01", "C02"}

    resolved, errors, used, warnings = _resolve_claim_citation_handles(
        "A claim [[claim:C01]] and another [[claim:C02]].",
        relations,
        authorable,
    )
    assert errors == []
    assert warnings == []
    assert "[REF:P01]" in resolved and "[REF:P02]" in resolved
    assert used == ["C01", "C02"]

    resolved, errors, used, warnings = _resolve_claim_citation_handles(
        "A fabricated [[claim:C99]].",
        relations,
        authorable,
    )
    assert resolved == "A fabricated ."
    assert errors == []
    assert used == []
    assert warnings == []


def test_local_synthesis_maps_claims_to_chunks_papers_permissions(
    tmp_path: Path,
) -> None:
    ctx = _make_wide_ctx(
        tmp_path, paper_count=4, chunks_per_paper=2, claim_count=4
    )
    graph = _build_asset_graph(ctx)
    relations = _build_local_evidence_relations(
        ctx, graph, ctx.section_data["claims"]
    )
    candidate = {
        "argument_plan": {
            "argument_flow": "Baseline to synthesis.",
            "paragraphs": [{
                "paragraph_index": 0,
                "function": "mechanism synthesis",
                "topic_sentence": "Mechanism governs the response.",
                "key_claims": ["C01", "C02"],
                "expected_word_count": 200,
            }],
        },
        "draft_text": "placeholder",
    }
    plan, packet, errors = _synthesize_local_candidate(
        candidate, relations, graph, {"C01", "C02", "C03", "C04"}
    )
    assert errors == []
    paragraph = plan["paragraphs"][0]
    assert paragraph["evidence_chunk_ids"] == ["K001", "K002", "K003", "K004"]
    assert paragraph["paper_ids"] == ["P01", "P02", "P03", "P04"]
    assert paragraph["writing_permission"] == "hedged_factual_assertion"
    assert len(packet["items"]) == 4
    assert {item["paper_id"] for item in packet["items"]} == {
        "P01", "P02", "P03", "P04",
    }
    assert packet["uncovered_claim_ids"] == ["C03", "C04"]


def test_local_synthesis_ignores_unknown_claim(tmp_path: Path) -> None:
    ctx = _make_wide_ctx(
        tmp_path, paper_count=2, chunks_per_paper=2, claim_count=2
    )
    graph = _build_asset_graph(ctx)
    relations = _build_local_evidence_relations(
        ctx, graph, ctx.section_data["claims"]
    )
    candidate = {
        "argument_plan": {
            "paragraphs": [{
                "key_claims": ["C99"],
            }],
        },
        "draft_text": "placeholder",
    }
    plan, packet, errors = _synthesize_local_candidate(
        candidate, relations, graph, {"C01", "C02"}
    )
    assert errors == []
    assert plan["paragraphs"][0]["key_claims"] == []
    assert packet["items"] == []


def test_statement_priority_uses_effective_statement_everywhere(
    tmp_path: Path,
) -> None:
    ctx = _make_wide_ctx(tmp_path, paper_count=1, chunks_per_paper=2, claim_count=1)
    graph = _build_asset_graph(ctx)
    claim = {
        **ctx.section_data["claims"][0],
        "effective_statement": "EFFECTIVE statement text",
        "supported_rewrite": "SUPPORTED rewrite text",
        "authoring_statement": "AUTHORING statement text",
        "statement": "BASE statement text",
    }
    relations = _build_local_evidence_relations(ctx, graph, [claim])
    relation = relations["claims"]["C01"]
    assert relation["statement"] == "EFFECTIVE statement text"

    card = _compact_claim_card(claim, relation)
    assert card["statement"] == "EFFECTIVE statement text"

    plan, packet, errors = _synthesize_local_candidate(
        {
            "argument_plan": {"paragraphs": [{"key_claims": ["C01"]}]},
            "draft_text": "placeholder",
        },
        relations,
        graph,
        {"C01"},
    )
    assert errors == []
    assert packet["items"][0]["support_hint"].startswith(
        "EFFECTIVE statement text"
    )


def _permission_ctx(tmp_path: Path):
    ctx = _make_wide_ctx(tmp_path, paper_count=1, chunks_per_paper=1, claim_count=1)
    graph = _build_asset_graph(ctx)
    return ctx, graph


def test_local_permission_open_claim_is_evidence_gap_only(
    tmp_path: Path,
) -> None:
    ctx, graph = _permission_ctx(tmp_path)
    claim = {
        **ctx.section_data["claims"][0],
        "claim_id": "OPEN",
        "strength": "open",
        "writing_permission": "evidence_gap_only",
        "supporting_text_chunk_ids": [],
        "supporting_chunk_ids": [],
        "factual_support_chunk_ids": [],
        "core_chunk_ids": [],
        "paper_ids": [],
    }
    relations = _build_local_evidence_relations(ctx, graph, [claim])
    assert relations["claims"]["OPEN"]["writing_permission"] == (
        "evidence_gap_only"
    )


def test_local_permission_interpretive_explicit_permission_wins(
    tmp_path: Path,
) -> None:
    ctx, graph = _permission_ctx(tmp_path)
    claim = {
        **ctx.section_data["claims"][0],
        "strength": "qualified",
        "writing_permission": "interpretive_synthesis",
    }
    relations = _build_local_evidence_relations(ctx, graph, [claim])
    assert relations["claims"]["C01"]["writing_permission"] == (
        "interpretive_synthesis"
    )


def test_local_permission_discovery_chunk_caps_to_evidence_gap_only(
    tmp_path: Path,
) -> None:
    ctx, graph = _permission_ctx(tmp_path)
    graph.chunks["K001"] = replace(
        graph.chunks["K001"],
        use_permission="discovery_only",
        content_depth="metadata",
        context_complete=False,
        route_provenance={"migration": "r3_2"},
    )
    claim = {
        **ctx.section_data["claims"][0],
        "strength": "established",
        "writing_permission": "factual_assertion",
    }
    relations = _build_local_evidence_relations(ctx, graph, [claim])
    assert relations["claims"]["C01"]["writing_permission"] == (
        "evidence_gap_only"
    )


def test_local_permission_contextual_chunk_caps_to_hedged(
    tmp_path: Path,
) -> None:
    ctx, graph = _permission_ctx(tmp_path)
    graph.chunks["K001"] = replace(
        graph.chunks["K001"],
        use_permission="contextual_or_qualified_support",
        scope_fit="contextual",
        route_provenance={"migration": "r3_2"},
    )
    claim = {
        **ctx.section_data["claims"][0],
        "strength": "established",
        "writing_permission": "factual_assertion",
    }
    relations = _build_local_evidence_relations(ctx, graph, [claim])
    assert relations["claims"]["C01"]["writing_permission"] == (
        "hedged_factual_assertion"
    )


def test_model_supplied_permission_can_only_downgrade(tmp_path: Path) -> None:
    ctx, graph = _permission_ctx(tmp_path)
    relations = _build_local_evidence_relations(
        ctx, graph, ctx.section_data["claims"]
    )
    assert relations["claims"]["C01"]["writing_permission"] == (
        "hedged_factual_assertion"
    )
    candidate = {
        "argument_plan": {
            "paragraphs": [{
                "key_claims": ["C01"],
                "writing_permission": "factual_assertion",
            }],
        },
        "draft_text": "placeholder",
    }
    plan, _, _ = _synthesize_local_candidate(
        candidate, relations, graph, {"C01"}
    )
    assert plan["paragraphs"][0]["writing_permission"] == (
        "hedged_factual_assertion"
    )
    candidate["argument_plan"]["paragraphs"][0]["writing_permission"] = (
        "evidence_gap_only"
    )
    plan, _, _ = _synthesize_local_candidate(
        candidate, relations, graph, {"C01"}
    )
    assert plan["paragraphs"][0]["writing_permission"] == "evidence_gap_only"


def test_open_claim_remains_authorable_without_citation(tmp_path: Path) -> None:
    ctx, graph = _permission_ctx(tmp_path)
    open_claim = {
        **ctx.section_data["claims"][0],
        "claim_id": "OPEN",
        "strength": "open",
        "writing_permission": "evidence_gap_only",
        "supporting_text_chunk_ids": [],
        "supporting_chunk_ids": [],
        "factual_support_chunk_ids": [],
        "core_chunk_ids": [],
        "paper_ids": [],
    }
    claims = [ctx.section_data["claims"][0], open_claim]
    relations = _build_local_evidence_relations(ctx, graph, claims)
    assert relations["claims"]["OPEN"]["writing_permission"] == (
        "evidence_gap_only"
    )
    plan, packet, errors = _synthesize_local_candidate(
        {
            "argument_plan": {
                "paragraphs": [{"key_claims": ["OPEN"]}],
            },
            "draft_text": "placeholder",
        },
        relations,
        graph,
        {"C01", "OPEN"},
    )
    assert errors == []
    paragraph = plan["paragraphs"][0]
    assert paragraph["evidence_chunk_ids"] == []
    assert paragraph["paper_ids"] == []
    assert paragraph["writing_permission"] == "evidence_gap_only"
    assert "OPEN" in packet["uncovered_claim_ids"]

    resolved, errors, _used, warnings = _resolve_claim_citation_handles(
        "An open question [[claim:OPEN]] remains unresolved.",
        relations,
        {"C01", "OPEN"},
    )
    assert errors == []
    assert "[[claim:OPEN]]" not in resolved
    assert warnings == []


def test_expansion_stops_when_diversity_meets_minimum(tmp_path: Path) -> None:
    ctx = _make_wide_ctx(
        tmp_path,
        paper_count=24,
        chunks_per_paper=1,
        claim_count=32,
        claim_support="pair_12",
    )
    graph = _build_asset_graph(ctx)
    selected = _select_adaptive_core_chunks(
        claims=ctx.section_data["claims"],
        graph=graph,
        portfolio_recommended=[f"K{i:03d}" for i in range(1, 13)],
        core_limit=12,
        min_limit=8,
        max_limit=16,
        minimum_synthesis_sources=12,
    )
    assert len(selected["chunk_ids"]) == 12
    assert selected["covered_claim_count"] == 32
    assert selected["unique_paper_count"] == 12
    assert selected["reason"] == "default"


def test_adaptive_bounds_are_normalized_coherently(tmp_path: Path) -> None:
    ctx = _make_wide_ctx(tmp_path)
    ctx.section_data = {
        **ctx.section_data,
        "authoring_core_chunk_min": 16,
        "authoring_core_chunk_limit": 12,
        "authoring_core_chunk_max": 8,
    }
    payload = _prepare(CompactSectionAuthoringToolProvider(ctx), tmp_path)
    bounds = payload["workspace_diagnostics"]["selection_bounds"]
    assert bounds["min"] == 8
    assert bounds["default"] == 12
    assert bounds["max"] == 16


def test_workspace_too_large_status_is_visible_not_silent_truncation(
    tmp_path: Path,
) -> None:
    ctx = _make_wide_ctx(
        tmp_path,
        paper_count=12,
        chunks_per_paper=1,
        claim_count=32,
        claim_support="quad",
        rich=True,
        min_limit=12,
    )
    ctx.section_data["compact_tool_result_limit"] = 2_000
    provider = CompactSectionAuthoringToolProvider(ctx)
    functions = {
        tool.name: tool._func for tool in provider.get_tools(tmp_path)
    }
    payload = json.loads(functions["prepare_authoring_workspace"]())
    assert payload["status"] == "workspace_too_large"
    assert payload["workspace_diagnostics"]["truncation_risk"] is True
    assert payload["workspace_diagnostics"]["estimated_token_size"] > 2_000
    assert payload["claim_cards"]
    assert "instruction" in payload
    second = json.loads(functions["prepare_authoring_workspace"]())
    assert second["status"] == "workspace_too_large"


def test_evidence_items_merge_same_chunk_paper_claim_ids(
    tmp_path: Path,
) -> None:
    ctx = _make_wide_ctx(
        tmp_path, paper_count=1, chunks_per_paper=2, claim_count=2,
        claim_support="shared",
    )
    graph = _build_asset_graph(ctx)
    relations = _build_local_evidence_relations(
        ctx, graph, ctx.section_data["claims"]
    )
    plan, packet, errors = _synthesize_local_candidate(
        {
            "argument_plan": {
                "paragraphs": [{"key_claims": ["C01", "C02"]}],
            },
            "draft_text": "placeholder",
        },
        relations,
        graph,
        {"C01", "C02"},
    )
    assert errors == []
    assert len(packet["items"]) == 1
    item = packet["items"][0]
    assert item["chunk_id"] == "K001"
    assert item["claim_ids"] == ["C01", "C02"]


def test_compact_prompt_has_no_hard_word_target() -> None:
    prompt = (
        Path(__file__).resolve().parents[1]
        / "prompts"
        / "roles"
        / "Compact Section Review Author.txt"
    ).read_text(encoding="utf-8")
    assert "at least ~600" not in prompt
    assert "1300" in prompt
    assert "evidence-limited shorter output is acceptable" in prompt
