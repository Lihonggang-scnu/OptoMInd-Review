from __future__ import annotations

import json
from pathlib import Path

import pytest

import optomind_research.runtime.compact_section_authoring as compact_authoring
from optomind_research.runtime.compact_section_authoring import (
    COMPACT_SECTION_AUTHORING_TOOL_NAMES,
    CompactSectionAuthoringToolProvider,
    _fill_candidate_evidence_defaults,
    _hard_audit_blockers,
    parse_candidate_json,
)
from tests.test_section_authoring_worker import _make_ctx


def _functions(provider: CompactSectionAuthoringToolProvider, work_dir: Path):
    return {tool.name: tool._func for tool in provider.get_tools(work_dir)}


def _valid_candidate() -> dict:
    draft = (
        "Two-dimensional semiconductors support strong third-order optical "
        "responses because their electronic states remain confined in an "
        "atomically thin geometry. MoS2 exhibits saturable absorption under "
        "pulsed excitation, including a reported saturation scale of 1.5 "
        "nJ/cm^2 [REF:paper_A]. Graphene provides a complementary mechanism: "
        "its Kerr nonlinearity enables ultrafast all-optical switching "
        "[REF:paper_B]. The pairing offers a useful review-level distinction "
        "between absorptive bleaching and refractive modulation without "
        "implying a universal response across all atomically thin materials. "
        "This bounded distinction supplies the mechanism-level basis for "
        "comparing material platforms in the following section."
    )
    return {
        "argument_plan": {
            "argument_flow": "baseline to mechanisms to bounded synthesis",
            "paragraphs": [{
                "paragraph_index": 0,
                "function": "mechanism synthesis",
                "topic_sentence": (
                    "Reduced dimensionality supports distinct nonlinear pathways."
                ),
                "key_claims": ["C01"],
                "evidence_chunk_ids": ["chunk_found_001", "chunk_mech_001"],
                "paper_ids": ["paper_A", "paper_B"],
                "writing_permission": "interpretive_synthesis",
                "expected_word_count": 120,
            }],
        },
        "evidence_packet": {
            "items": [
                {
                    "chunk_id": "chunk_found_001",
                    "paper_id": "paper_A",
                    "claim_ids": ["C01"],
                    "writing_permission": "factual_assertion",
                    "support_hint": "saturable absorption 1.5 nJ/cm^2",
                    "literature_role": "foundation",
                    "scope_fit": "direct",
                    "not_usable_for": [],
                },
                {
                    "chunk_id": "chunk_mech_001",
                    "paper_id": "paper_B",
                    "claim_ids": ["C01"],
                    "writing_permission": "interpretive_synthesis",
                    "support_hint": "Kerr nonlinearity all-optical switching",
                    "literature_role": "mechanism",
                    "scope_fit": "direct",
                    "not_usable_for": [],
                },
            ],
            "uncovered_claim_ids": [],
        },
        "draft_text": draft,
    }


def _read_pointer(work_dir: Path) -> dict:
    return json.loads(
        (work_dir / "LAST_VALID_SECTION_POINTER.json").read_text(encoding="utf-8")
    )


def _make_binding_ctx(tmp_path: Path):
    """Ctx whose claims carry canonical chunk bindings for local synthesis."""

    ctx = _make_ctx(tmp_path)
    ctx.section_data = {
        **ctx.section_data,
        "claims": [
            {
                "claim_id": "C01",
                "statement": (
                    "MoS2 exhibits saturable absorption at 1.5 nJ/cm^2."
                ),
                "strength": "qualified",
                "writing_permission": "hedged_factual_assertion",
                "importance": "load_bearing",
                "evidence_type": "measurement",
                "claim_kind": "direct_fact",
                "supporting_text_chunk_ids": ["chunk_found_001"],
                "supporting_chunk_ids": ["chunk_found_001"],
                "factual_support_chunk_ids": ["chunk_found_001"],
                "core_chunk_ids": ["chunk_found_001"],
                "core_paper_ids": ["paper_A"],
                "paper_ids": ["paper_A"],
                "support_classification": "supported",
            },
            {
                "claim_id": "C02",
                "statement": (
                    "Graphene's Kerr nonlinearity enables ultrafast "
                    "all-optical switching."
                ),
                "strength": "qualified",
                "writing_permission": "hedged_factual_assertion",
                "importance": "load_bearing",
                "evidence_type": "measurement",
                "claim_kind": "direct_fact",
                "supporting_text_chunk_ids": ["chunk_mech_001"],
                "supporting_chunk_ids": ["chunk_mech_001"],
                "factual_support_chunk_ids": ["chunk_mech_001"],
                "core_chunk_ids": ["chunk_mech_001"],
                "core_paper_ids": ["paper_B"],
                "paper_ids": ["paper_B"],
                "support_classification": "supported",
            },
            {
                "claim_id": "C03",
                "statement": (
                    "Whether the mechanism transfers to the untested regime "
                    "remains an open question for future work."
                ),
                "strength": "open",
                "writing_permission": "evidence_gap_only",
                "importance": "supporting",
                "evidence_type": "open_question",
                "claim_kind": "frontier_uncertainty",
                "supporting_text_chunk_ids": [],
                "supporting_chunk_ids": [],
                "factual_support_chunk_ids": [],
                "core_chunk_ids": [],
                "core_paper_ids": [],
                "paper_ids": [],
                "support_classification": "open_question",
            },
        ],
        "authoring_core_chunk_limit": 12,
        "authoring_core_chunk_min": 8,
        "authoring_core_chunk_max": 16,
        "compact_workspace_target_tokens": 25_000,
        "compact_tool_result_limit": 32_000,
    }
    return ctx


def _local_candidate(*, key_claims: list[str] | None = None) -> dict:
    draft = (
        "MoS2 exhibits saturable absorption under pulsed excitation at a "
        "reported saturation scale of 1.5 nJ/cm^2 [[claim:C01]]. Graphene "
        "provides a complementary mechanism: its Kerr nonlinearity enables "
        "ultrafast all-optical switching [[claim:C02]]. The pairing offers a "
        "useful review-level distinction between absorptive bleaching and "
        "refractive modulation without implying a universal response across "
        "all atomically thin materials. This bounded distinction supplies the "
        "mechanism-level basis for comparing material platforms in the "
        "following section."
    )
    return {
        "argument_plan": {
            "argument_flow": "baseline to mechanisms to bounded synthesis",
            "paragraphs": [{
                "paragraph_index": 0,
                "function": "mechanism synthesis",
                "topic_sentence": (
                    "Reduced dimensionality supports distinct nonlinear pathways."
                ),
                "key_claims": key_claims or ["C01", "C02"],
                "expected_word_count": 120,
            }],
        },
        "draft_text": draft,
    }


def test_compact_workspace_batches_diverse_material_once(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    provider = CompactSectionAuthoringToolProvider(ctx)
    functions = _functions(provider, tmp_path)

    payload = json.loads(functions["prepare_authoring_workspace"]())

    assert provider.get_allowed_tool_names() == COMPACT_SECTION_AUTHORING_TOOL_NAMES
    assert payload["status"] == "ok"
    assert set(payload["retrieved_chunks"]) == {
        "chunk_found_001",
        "chunk_mech_001",
    }
    assert payload["materials"]["role_detail"]["foundation"].get(
        "chunk_ids"
    ) is None
    assert len(json.dumps(payload, ensure_ascii=False)) < 40_000


def test_compact_workspace_second_prepare_is_bounded_instruction_only(
    tmp_path: Path,
) -> None:
    ctx = _make_ctx(tmp_path)
    provider = CompactSectionAuthoringToolProvider(ctx)
    prepare = _functions(provider, tmp_path)["prepare_authoring_workspace"]

    first = json.loads(prepare())
    second_raw = prepare()
    second = json.loads(second_raw)

    assert first["status"] == "ok"
    assert second["status"] == "already_prepared"
    assert "retrieved_chunks" not in second
    assert len(second_raw) < 500


def test_compact_workspace_prioritizes_claim_specific_measurement_anchor(
    tmp_path: Path,
) -> None:
    ctx = _make_ctx(tmp_path)
    ctx.section_data = {
        **ctx.section_data,
        "claims": [{
            "claim_id": "C01",
            "statement": "MoS2 exhibits saturable absorption at 1.5 nJ/cm^2.",
            "load_bearing": True,
            "supporting_text_chunk_ids": ["chunk_mech_001", "chunk_found_001"],
            "evidence_component_map": [{
                "component": "reported saturation scale",
                "chunk_ids": ["chunk_mech_001", "chunk_found_001"],
            }],
        }],
    }
    payload = json.loads(
        _functions(CompactSectionAuthoringToolProvider(ctx), tmp_path)[
            "prepare_authoring_workspace"
        ]()
    )

    assert payload["retrieval_diagnostics"]["claim_anchor_chunk_ids"][0] == "chunk_found_001"
    mapping = payload["claim_evidence_map"][0]
    assert mapping["claim_id"] == "C01"
    assert "chunk_found_001" in mapping["recommended_chunk_ids"]


def test_compact_candidate_keeps_all_canonical_quality_gates(
    tmp_path: Path,
) -> None:
    ctx = _make_ctx(tmp_path)
    functions = _functions(CompactSectionAuthoringToolProvider(ctx), tmp_path)
    assert json.loads(functions["prepare_authoring_workspace"]())["status"] == "ok"

    candidate = _valid_candidate()

    result = json.loads(
        functions["submit_authoring_candidate"](
            json.dumps(candidate, ensure_ascii=False)
        )
    )

    assert result["status"] == "completed"
    assert "VALIDATION_PASSED" in result["validation"]
    assert result["audit"]["blocking_flags"] == 0
    assert (tmp_path / "SECTION_AUTHORING_PACKAGE.json").is_file()


def test_hard_audit_blockers_scan_beyond_the_twelve_flag_batch_cap():
    audit = {
        "status": "ok",
        "flags_detail": [
            {"flag_type": "overclaim", "severity": "blocking", "resolved": False}
            for _ in range(12)
        ] + [
            {
                "flag_type": "unknown_ref",
                "severity": "blocking",
                "resolved": False,
            }
        ],
    }

    blockers = _hard_audit_blockers(audit)

    assert [item["flag_type"] for item in blockers] == ["unknown_ref"]


def test_hard_audit_blockers_ignore_resolved_or_non_blocking_flags():
    audit = {
        "flags_detail": [
            {"flag_type": "unknown_ref", "severity": "blocking", "resolved": True},
            {"flag_type": "unknown_ref", "severity": "important", "resolved": False},
            {"flag_type": "overclaim", "severity": "blocking", "resolved": False},
        ],
    }

    assert _hard_audit_blockers(audit) == []


def test_candidate_evidence_defaults_copy_exact_plan_claim_ids():
    candidate = _valid_candidate()
    for item in candidate["evidence_packet"]["items"]:
        item["claim_ids"] = []

    packet = _fill_candidate_evidence_defaults(candidate)

    assert [item["claim_ids"] for item in packet["items"]] == [
        ["C01"],
        ["C01"],
    ]


def test_candidate_evidence_defaults_do_not_replace_supplied_claim_ids():
    candidate = _valid_candidate()
    candidate["evidence_packet"]["items"][0]["claim_ids"] = ["C99"]

    packet = _fill_candidate_evidence_defaults(candidate)

    assert packet["items"][0]["claim_ids"] == ["C99"]


def test_plan_rejection_preserves_pointer_and_candidate_files(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    functions = _functions(CompactSectionAuthoringToolProvider(ctx), tmp_path)
    assert json.loads(functions["prepare_authoring_workspace"]())["status"] == "ok"

    candidate = _valid_candidate()
    candidate["argument_plan"]["paragraphs"][0]["evidence_chunk_ids"] = [
        "chunk_unknown_999"
    ]
    candidate["argument_plan"]["paragraphs"][0]["paper_ids"] = ["paper_A"]

    result = json.loads(
        functions["submit_authoring_candidate"](
            json.dumps(candidate, ensure_ascii=False)
        )
    )

    assert result["status"] == "repair_required"
    assert result["stage"] == "argument_plan"
    assert result["last_valid_candidate"]["saved"] is True
    # Candidate 0 is provisional prose; the citation-audited snapshot is the
    # durable repair baseline when argument-plan validation rejects later.
    assert result["last_valid_candidate"]["candidate_index"] == 1
    assert result["last_valid_candidate"]["validation_level"] == "citation_audited"
    assert not (tmp_path / "SECTION_ARGUMENT_PLAN.json").exists()

    pointer = _read_pointer(tmp_path)
    assert pointer["candidate_index"] == 1
    assert pointer["validation_level"] == "citation_audited"
    candidate_dir = tmp_path / pointer["candidate_dir"]
    assert candidate_dir.is_dir()
    for name in (
        "CANDIDATE_MANIFEST.json",
        "SECTION_AUTHORING_CONTEXT.json",
        "SECTION_DRAFT_EN.md",
        "SECTION_EVIDENCE_PACKET.json",
        "SECTION_CITATION_MAP.json",
        "SECTION_AUTHORING_AUDIT.json",
    ):
        assert (candidate_dir / name).is_file(), name
    assert (tmp_path / "LAST_VALID_SECTION_STATE.json").is_file()
    preserved = (tmp_path / "LAST_VALID_SECTION_DRAFT_EN.md").read_text(
        encoding="utf-8"
    )
    assert preserved.strip() == candidate["draft_text"].strip()


def test_unknown_ref_preserves_provisional_body_for_repair(
    tmp_path: Path,
) -> None:
    ctx = _make_ctx(tmp_path)
    functions = _functions(CompactSectionAuthoringToolProvider(ctx), tmp_path)
    assert json.loads(functions["prepare_authoring_workspace"]())["status"] == "ok"

    candidate = _valid_candidate()
    candidate["draft_text"] = candidate["draft_text"].replace(
        "[REF:paper_A]", "[REF:paper_unknown]"
    )

    result = json.loads(
        functions["submit_authoring_candidate"](
            json.dumps(candidate, ensure_ascii=False)
        )
    )

    assert result["status"] == "revision_required"
    assert result["stage"] == "citation_audit"
    assert result["audit"]["blocking_flags"] >= 1
    assert any(
        item.get("flag_type") == "unknown_ref"
        for item in result["audit"]["blocking_batch"]
    )
    # A hard provenance failure remains blocking, but it must not erase the
    # first non-empty body.  Before the transaction change this assertion was
    # red: the rollback deleted both live and last-valid draft artifacts.
    assert result["last_valid_candidate"]["saved"] is True
    assert result["last_valid_candidate"]["reason"] == (
        "hard_citation_audit_failure"
    )
    assert result["last_valid_candidate"]["provisional_draft_preserved"] is True
    assert (tmp_path / "LAST_VALID_SECTION_POINTER.json").is_file()
    assert (tmp_path / "_last_valid").is_dir()
    assert (tmp_path / "SECTION_DRAFT_EN.md").is_file()
    assert "paper_unknown" in (tmp_path / "SECTION_DRAFT_EN.md").read_text(
        encoding="utf-8"
    )


def test_normal_completed_candidate_remains_completed(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    functions = _functions(CompactSectionAuthoringToolProvider(ctx), tmp_path)
    assert json.loads(functions["prepare_authoring_workspace"]())["status"] == "ok"

    result = json.loads(
        functions["submit_authoring_candidate"](
            json.dumps(_valid_candidate(), ensure_ascii=False)
        )
    )

    assert result["status"] == "completed"
    assert "VALIDATION_PASSED" in result["validation"]
    assert result["audit"]["blocking_flags"] == 0
    # The provisional body is persisted before evidence binding, then the
    # fully audited candidate supersedes it.
    assert result["last_valid_candidate"]["candidate_index"] == 2
    assert result["last_valid_candidate"]["validation_level"] == "plan_validated"

    pointer = _read_pointer(tmp_path)
    assert pointer["candidate_index"] == 2
    assert pointer["validation_level"] == "plan_validated"
    candidate_dir = tmp_path / pointer["candidate_dir"]
    assert (candidate_dir / "CANDIDATE_MANIFEST.json").is_file()
    assert (candidate_dir / "SECTION_ARGUMENT_PLAN.json").is_file()
    assert (candidate_dir / "SECTION_DRAFT_EN.md").is_file()


def test_pointer_and_candidate_indices_remain_monotonic(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    functions = _functions(CompactSectionAuthoringToolProvider(ctx), tmp_path)
    assert json.loads(functions["prepare_authoring_workspace"]())["status"] == "ok"
    candidate_json = json.dumps(_valid_candidate(), ensure_ascii=False)

    first = json.loads(functions["submit_authoring_candidate"](candidate_json))
    assert first["status"] == "completed"
    first_index = first["last_valid_candidate"]["candidate_index"]

    second = json.loads(functions["submit_authoring_candidate"](candidate_json))
    assert second["status"] == "completed"
    second_index = second["last_valid_candidate"]["candidate_index"]

    assert second_index > first_index
    pointer = _read_pointer(tmp_path)
    assert pointer["candidate_index"] == second_index
    state = json.loads(
        (tmp_path / "LAST_VALID_SECTION_STATE.json").read_text(encoding="utf-8")
    )
    assert state["candidate_index"] == second_index

    indices = []
    for index in range(pointer["candidate_index"] + 1):
        candidate_dir = tmp_path / "_last_valid" / f"candidate-{index:04d}"
        assert candidate_dir.is_dir()
        manifest = json.loads(
            (candidate_dir / "CANDIDATE_MANIFEST.json").read_text(encoding="utf-8")
        )
        assert manifest["candidate_index"] == index
        indices.append(manifest["candidate_index"])
    assert indices == list(range(pointer["candidate_index"] + 1))


def test_local_submission_completes_without_model_relation_tables(
    tmp_path: Path,
) -> None:
    ctx = _make_binding_ctx(tmp_path)
    functions = _functions(CompactSectionAuthoringToolProvider(ctx), tmp_path)
    assert json.loads(functions["prepare_authoring_workspace"]())["status"] == "ok"

    result = json.loads(
        functions["submit_authoring_candidate"](
            json.dumps(_local_candidate(), ensure_ascii=False)
        )
    )

    assert result["status"] == "completed"
    plan = json.loads(
        (tmp_path / "SECTION_ARGUMENT_PLAN.json").read_text(encoding="utf-8")
    )
    paragraph = plan["paragraphs"][0]
    assert paragraph["evidence_chunk_ids"] == [
        "chunk_found_001",
        "chunk_mech_001",
    ]
    assert paragraph["paper_ids"] == ["paper_A", "paper_B"]
    assert paragraph["writing_permission"] == "hedged_factual_assertion"
    packet = json.loads(
        (tmp_path / "SECTION_EVIDENCE_PACKET.json").read_text(encoding="utf-8")
    )
    assert {(item["chunk_id"], item["paper_id"]) for item in packet["items"]} == {
        ("chunk_found_001", "paper_A"),
        ("chunk_mech_001", "paper_B"),
    }
    draft = (tmp_path / "SECTION_DRAFT_EN.md").read_text(encoding="utf-8")
    assert "[[claim:" not in draft
    assert "[REF:paper_A]" in draft and "[REF:paper_B]" in draft


def test_local_submission_records_omitted_claims_locally(tmp_path: Path) -> None:
    ctx = _make_binding_ctx(tmp_path)
    functions = _functions(CompactSectionAuthoringToolProvider(ctx), tmp_path)
    assert json.loads(functions["prepare_authoring_workspace"]())["status"] == "ok"

    result = json.loads(
        functions["submit_authoring_candidate"](
            json.dumps(_local_candidate(key_claims=["C01"]), ensure_ascii=False)
        )
    )

    assert result["status"] == "completed"
    assert result["omitted_claim_ids"] == ["C02", "C03"]
    omitted = json.loads(
        (tmp_path / "SECTION_OMITTED_CLAIMS.json").read_text(encoding="utf-8")
    )
    assert omitted["omitted_claim_ids"] == ["C02", "C03"]
    assert omitted["count"] == 2
    packet = json.loads(
        (tmp_path / "SECTION_EVIDENCE_PACKET.json").read_text(encoding="utf-8")
    )
    assert packet["uncovered_claim_ids"] == ["C02", "C03"]


def test_local_submission_ignores_unknown_claim_without_fabricating_evidence(
    tmp_path: Path,
) -> None:
    ctx = _make_binding_ctx(tmp_path)
    functions = _functions(CompactSectionAuthoringToolProvider(ctx), tmp_path)
    assert json.loads(functions["prepare_authoring_workspace"]())["status"] == "ok"

    candidate = _local_candidate(key_claims=["C99"])
    result = json.loads(
        functions["submit_authoring_candidate"](
            json.dumps(candidate, ensure_ascii=False)
        )
    )

    assert result["stage"] != "local_relations"
    assert not result.get("errors")
    assert (tmp_path / "SECTION_DRAFT_EN.md").exists()
    packet = json.loads(
        (tmp_path / "SECTION_EVIDENCE_PACKET.json").read_text(
            encoding="utf-8"
        )
    )
    assert packet["items"] == []


def test_local_synthesis_error_preserves_provisional_english_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _make_binding_ctx(tmp_path)
    functions = _functions(CompactSectionAuthoringToolProvider(ctx), tmp_path)
    assert json.loads(functions["prepare_authoring_workspace"]())["status"] == "ok"

    monkeypatch.setattr(
        compact_authoring,
        "_synthesize_local_candidate",
        lambda *args, **kwargs: (None, None, ["synthetic relation failure"]),
    )
    candidate = _local_candidate()
    result = json.loads(
        functions["submit_authoring_candidate"](
            json.dumps(candidate, ensure_ascii=False)
        )
    )

    assert result["status"] == "repair_required"
    assert result["stage"] == "local_relations"
    assert result["provisional_draft_preserved"] is True
    assert (tmp_path / "SECTION_DRAFT_EN.md").read_text(
        encoding="utf-8"
    ).strip() == candidate["draft_text"].strip()
    assert (tmp_path / "LAST_VALID_SECTION_DRAFT_EN.md").is_file()


def test_local_submission_keeps_open_claim_authorable_without_citation(
    tmp_path: Path,
) -> None:
    ctx = _make_binding_ctx(tmp_path)
    functions = _functions(CompactSectionAuthoringToolProvider(ctx), tmp_path)
    assert json.loads(functions["prepare_authoring_workspace"]())["status"] == "ok"

    candidate = _local_candidate()
    candidate["argument_plan"]["paragraphs"].append({
        "paragraph_index": 1,
        "function": "open question",
        "topic_sentence": "The transfer boundary remains unresolved.",
        "key_claims": ["C03"],
        "expected_word_count": 60,
    })
    candidate["draft_text"] = (
        candidate["draft_text"]
        + " Whether the mechanism transfers to the untested regime remains an "
        "open question for future work [[claim:C03]]."
    )

    result = json.loads(
        functions["submit_authoring_candidate"](
            json.dumps(candidate, ensure_ascii=False)
        )
    )

    assert result["status"] == "completed"
    assert result["local_relation_warnings"] == []
    plan = json.loads(
        (tmp_path / "SECTION_ARGUMENT_PLAN.json").read_text(encoding="utf-8")
    )
    open_paragraph = plan["paragraphs"][1]
    assert open_paragraph["key_claims"] == ["C03"]
    assert open_paragraph["evidence_chunk_ids"] == []
    assert open_paragraph["paper_ids"] == []
    assert open_paragraph["writing_permission"] == "evidence_gap_only"
    packet = json.loads(
        (tmp_path / "SECTION_EVIDENCE_PACKET.json").read_text(encoding="utf-8")
    )
    assert "C03" in packet["uncovered_claim_ids"]
    draft = (tmp_path / "SECTION_DRAFT_EN.md").read_text(encoding="utf-8")
    assert "[[claim:C03]]" not in draft


def test_candidate_json_repairs_single_premature_root_close():
    malformed = (
        '{"argument_plan":{"paragraphs":[{"index":0}]}},'
        ' "evidence_packet":{"items":[]}, "draft_text":"ok"}'
    )

    parsed, repair = parse_candidate_json(malformed)

    assert parsed["argument_plan"]["paragraphs"][0]["index"] == 0
    assert parsed["evidence_packet"] == {"items": []}
    assert parsed["draft_text"] == "ok"
    assert repair is not None
    assert repair["event"] == "premature_root_close_repaired"


def test_candidate_json_rejects_ambiguous_trailing_data():
    ambiguous = (
        '{"argument_plan":{"paragraphs":[{"index":0}]}} trailing_not_json'
    )

    with pytest.raises(ValueError):
        parse_candidate_json(ambiguous)
