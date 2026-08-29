"""Focused offline tests for representative-application garnish in the
chapter asset enhancer.

Representative applications are an additive trust plane: the explanatory
planner's representative rows are delegated to a single batched cheap-Qwen
writer, local metadata is searched first with bounded per-backend results,
S2 is a capped fallback, and missing or malformed examples always fail open.
Selected records are merged into the canonical top-level explanatory ledger
so downstream handoff and metadata resolution see their REF markers.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

import optomind_research.chapter_asset_enhancer as enhancer
import scripts.run_chapter_asset_enhancer as cli


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory (mirrors existing enhancer tests)."""
    base = (
        Path(tempfile.gettempdir())
        / "optomind-chapter-asset-enhancer-app-tests"
    )
    base.mkdir(exist_ok=True)
    path = base / f"{request.node.name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def _fake_packet_data() -> dict:
    return {
        "section_id": "S01",
        "section_contract": {
            "title": "Physics-Informed Neural Networks",
            "section_purpose": "Establish the mechanism and boundaries of PINNs.",
            "central_thesis": "PINNs translate governing equations into trainable residual constraints.",
            "argument_role": "Define, explain, and apply the method.",
            "forbidden_overclaims": ["Do not claim general superiority."],
            "scope_guardrails": ["Stay inside the reviewed electromagnetic and heat-transfer cases."],
            "open_questions": ["Which boundary conditions remain difficult to recover?"],
        },
        "claims": [
            {
                "claim_id": "C1",
                "statement": "PINNs embed governing equations as residual loss terms.",
                "statement_for_writing": "The reviewed studies report that PINNs embed governing equations as residual loss terms.",
                "writing_permission": "factual_assertion",
                "evidence_binding_status": "direct",
                "claim_state": "ready_for_write",
                "supported_components": ["loss residual"],
                "missing_evidence_components": [],
                "caveats": [],
            },
            {
                "claim_id": "C2",
                "statement": "PINNs recover missing boundary conditions in heat transfer.",
                "statement_for_writing": "The reviewed study reports that PINNs recover missing boundary conditions in heat transfer.",
                "writing_permission": "hedged_factual_assertion",
                "evidence_binding_status": "direct",
                "claim_state": "ready_for_write",
                "supported_components": ["boundary recovery"],
                "missing_evidence_components": [],
                "caveats": ["Limited to the demonstrated benchmark."],
            },
        ],
        "evidence_packets": [
            {
                "claim_id": "C1",
                "paper_id": "paper-s2-001",
                "chunk_id": "s2chunk:001",
                "exact_spans": ["PINNs embed governing equations as residual penalty terms in the training loss."],
                "visual_refs": [],
                "support_relation": "component_support",
                "limitations": ["Do not generalize to all physics-augmented methods."],
                "evidence_level": "structured_snippet",
                "source_kind": "s2_body_snippet",
                "scope_fit": "in_domain",
                "retrieval_role": "evidence_candidate",
                "source_title": "Physics-Informed Neural Network Survey",
            },
            {
                "claim_id": "C2",
                "paper_id": "paper-ht-003",
                "chunk_id": "fulltext:003",
                "exact_spans": ["PINNs recover missing boundary conditions from sparse sensor data."],
                "visual_refs": [],
                "support_relation": "component_support",
                "limitations": ["Demonstrated on a heat-transfer benchmark."],
                "evidence_level": "fulltext",
                "source_kind": "fulltext",
                "scope_fit": "in_domain",
                "retrieval_role": "evidence_candidate",
                "source_title": "Heat Transfer Boundary Recovery",
            },
        ],
        "contradictions": [],
        "open_questions": ["Which boundary conditions remain difficult to recover?"],
        "transition_contract": {},
        "uncited_load_bearing_claim_ids": [],
        "visual_evidence": [],
        "visual_gap_plan": [],
        "manuscript_context": {},
        "literature_coverage": {},
    }


def _write_sources(tmp_path: Path) -> tuple[Path, Path]:
    packet_path = tmp_path / "input_packet.json"
    old_path = tmp_path / "SECTION_DRAFT_EN.md"
    packet_path.write_text(
        json.dumps(_fake_packet_data(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    old_path.write_text(
        "PINNs embed governing equations in the loss. "
        "They also recover boundary conditions from sparse data.",
        encoding="utf-8",
    )
    return packet_path, old_path


def _ledger_for_fake_packet() -> enhancer.HandleLedger:
    packet = enhancer._rehydrate_packet(_fake_packet_data())
    return enhancer._build_handle_ledger(packet)


def _natural_application_prose(row: dict) -> str:
    return (
        "One representative application used the method to recover "
        "heat-conduction boundary values from sparse sensor measurements, "
        "reporting accurate recovery in the demonstrated benchmark."
    )


def _application_fake_caller(
    ledger: enhancer.HandleLedger,
    *,
    block_count: int = 1,
    block_prose: str | list[str] | None = None,
    explanatory_rows: list[dict] | None = None,
    supplemental_explanatory_rows: list[dict] | None = None,
    supplemental_raise: bool = False,
    writer_prose: str | None = None,
    writer_rows: list[dict] | None = None,
    writer_raise: bool = False,
    writer_raise_on_batch: int | None = None,
):
    c1 = ledger.claim_by_id["C1"].handle
    e1 = ledger.evidence_by_claim["C1"][0].handle
    default_prose = (
        "The PINN method embeds governing equations as residual losses. "
        "This makes the approach trainable on sparse sensor data."
    )
    if block_prose is None:
        block_prose = [default_prose] * block_count
    elif isinstance(block_prose, str):
        block_prose = [block_prose] * block_count
    block_writer_calls = {"value": 0}
    calls: list[str] = []
    planner_calls = {"count": 0}
    planner_payloads: list[dict] = []
    planner_modes: list[str] = []
    writer_payloads: list[dict] = []

    def caller(agent_name: str, messages: list[dict], **_kwargs):
        calls.append(agent_name)
        usage = {
            "model_name": "qwen3.7-flash",
            "input_tokens": 80,
            "output_tokens": 40,
        }
        if agent_name == "ChapterAssetArgumentPlannerAgent":
            content = json.dumps(
                {
                    "chapter_thesis": "Thesis.",
                    "reader_takeaway": "Takeaway.",
                    "argument_sequence": [],
                    "terminology_rows": [],
                    "explanation_block_rows": [
                        {
                            "block_index": index + 1,
                            "title": f"Method block {index + 1}",
                            "block_type": "explanatory_body",
                            "goal": (
                                "Explain the PINN method and its "
                                "implementation."
                            ),
                            "claim_handles": [c1],
                            "evidence_handles": [e1],
                            "omitted_handle_reasons": {},
                        }
                        for index in range(block_count)
                    ],
                    "omitted_handle_reasons": {},
                }
            )
        elif agent_name == "ChapterAssetExplanationBlockWriterAgent":
            index = block_writer_calls["value"]
            block_writer_calls["value"] += 1
            prose = block_prose[index] if index < len(block_prose) else (
                default_prose
            )
            content = json.dumps(
                {
                    "paragraph_prose": prose,
                    "used_evidence_handles": [e1],
                }
            )
        elif agent_name == "ChapterAssetLegacyGapAuditorAgent":
            content = json.dumps(
                {
                    "verdict": "no_actionable_gaps",
                    "gaps": [],
                    "notes": "",
                }
            )
        elif agent_name == "ChapterAssetBlockScientificReviewerAgent":
            content = json.dumps({"review_rows": [], "notes": ""})
        elif agent_name == "ChapterAssetExplanatoryCitationPlannerAgent":
            planner_calls["count"] += 1
            payload = json.loads(messages[-1]["content"])
            planner_payloads.append(payload)
            mode = str(
                (payload.get("representative_application_policy") or {}).get(
                    "mode", "initial"
                )
            )
            planner_modes.append(mode)
            if (
                mode == "supplemental_representative_applications"
                and supplemental_raise
            ):
                raise RuntimeError("supplemental planner transport failure")
            if (
                mode == "supplemental_representative_applications"
                and supplemental_explanatory_rows is not None
            ):
                rows = supplemental_explanatory_rows
            else:
                rows = explanatory_rows
            content = json.dumps(
                {"explanatory_rows": list(rows or [])}
            )
        elif agent_name == "ChapterAssetExplanatorySemanticRerankerAgent":
            payload = json.loads(messages[-1]["content"])
            content = json.dumps(
                {
                    "semantic_scores": [
                        {
                            "handle": row["handle"],
                            "helpfulness_score": 85,
                            "reason": "relevant",
                        }
                        for row in payload["candidate_table"]
                    ]
                }
            )
        elif agent_name == "ChapterAssetRepresentativeApplicationWriterAgent":
            if writer_raise:
                raise RuntimeError("writer transport failure")
            payload = json.loads(messages[-1]["content"])
            writer_payloads.append(payload)
            if (
                writer_raise_on_batch is not None
                and int(
                    (payload.get("batch") or {}).get("index") or 0
                )
                == writer_raise_on_batch
            ):
                raise RuntimeError("writer batch transport failure")
            if writer_rows is not None:
                content = json.dumps({"application_rows": writer_rows})
            elif writer_prose is not None:
                content = json.dumps(
                    {
                        "application_rows": [
                            {
                                "target_handle": row["target_handle"],
                                "prose": writer_prose,
                            }
                            for row in payload["targets"]
                        ]
                    }
                )
            else:
                content = json.dumps(
                    {
                        "application_rows": [
                            {
                                "target_handle": row["target_handle"],
                                "prose": _natural_application_prose(row),
                            }
                            for row in payload["targets"]
                        ]
                    }
                )
        else:
            raise AssertionError(f"Unexpected agent: {agent_name}")
        return {"content": content, "_llm_usage": usage}

    caller.planner_payloads = planner_payloads  # type: ignore[attr-defined]
    caller.planner_modes = planner_modes  # type: ignore[attr-defined]
    caller.writer_payloads = writer_payloads  # type: ignore[attr-defined]
    return caller, calls


def _run_with(
    tmp_path: Path,
    caller,
    *,
    local_search,
    s2_search=None,
    name: str = "out",
    **kwargs,
):
    packet_path, old_path = _write_sources(tmp_path)
    return enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / name,
        live=True,
        qwen_caller=caller,
        local_search_callback=local_search,
        s2_search_callback=s2_search,
        **kwargs,
    )


def _read_ledger(tmp_path: Path, name: str = "out") -> dict:
    return json.loads(
        (
            tmp_path
            / name
            / "EXPLANATORY_CITATION_LEDGER.json"
        ).read_text(encoding="utf-8")
    )


def _read_enhanced(tmp_path: Path, name: str = "out") -> str:
    return (
        tmp_path / name / "ENHANCED_CHAPTER.md"
    ).read_text(encoding="utf-8")


def _good_local_candidate(
    *,
    paper_id: str = "app-local-1",
    title: str = "PINN Heat Conduction Case Study",
    doi: str = "10.1/pinn-heat",
    abstract: str = (
        "A PINN implementation recovered heat conduction boundary values "
        "from sparse sensor data."
    ),
    relevance_score: float = 0.9,
) -> list[dict]:
    return [
        {
            "paper_id": paper_id,
            "title": title,
            "authors": ["A. Author"],
            "year": 2024,
            "doi": doi,
            "abstract": abstract,
            "venue": "Local Journal",
            "relevance_score": relevance_score,
        }
    ]


def test_application_planned_rows_delegated_from_explanatory_planner(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, calls = _application_fake_caller(
        ledger,
        block_count=2,
        explanatory_rows=[
            {
                "block_index": 1,
                "target_sentence": "This makes the approach trainable on "
                "sparse sensor data.",
                "benefit_type": "mechanism_background",
                "query": "PINN residual loss background",
            },
            {
                "block_index": 2,
                "target_sentence": "This makes the approach trainable on "
                "sparse sensor data.",
                "benefit_type": "representative_application",
                "query": "PINN heat conduction application",
            },
        ],
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        if "background" in query.lower():
            return _good_local_candidate(
                paper_id="explanatory-paper",
                title="PINN Background Theory",
                doi="10.1/background",
                abstract="PINN background theory on residual losses.",
            )
        return _good_local_candidate(
            paper_id="app-paper",
            title="PINN Heat Transfer Application",
            doi="10.1/app-case",
            abstract=(
                "A PINN application recovered heat conduction boundaries "
                "from sparse sensor data."
            ),
        )

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-delegated",
    )

    ledger_data = _read_ledger(tmp_path, "out-delegated")
    applications = ledger_data["representative_applications"]
    metrics = applications["metrics"]
    assert metrics["target_source"] == "explanatory_planner"
    assert metrics["fallback_used"] is False
    assert metrics["targets_planned"] == 1
    assert metrics["examples_attached"] == 1
    assert metrics["application_writer_call_count"] == 1
    assert "ChapterAssetRepresentativeApplicationWriterAgent" in calls
    # The generic explanatory path did not create a record for block 2.
    block2_definition_records = [
        record
        for record in ledger_data["records"]
        if "definition" in record.get("benefit_types", [])
    ]
    assert len(block2_definition_records) == 0
    app_records = [
        record
        for record in ledger_data["records"]
        if "representative_application" in record.get("benefit_types", [])
    ]
    assert len(app_records) == 1
    assert app_records[0]["marker_id"] == "doi:10.1/app-case"
    assert app_records[0]["permission"] == "background_explanation_only"
    assert report["status"] == "enhanced"
    assert (
        "One representative application used the method"
        in _read_enhanced(tmp_path, "out-delegated")
    )
    assert "[REF:doi:10.1/app-case]" in _read_enhanced(
        tmp_path, "out-delegated"
    )


def test_application_deterministic_fallback_when_planner_has_no_app_rows(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, _calls = _application_fake_caller(
        ledger,
        explanatory_rows=[
            {
                "block_index": 1,
                "target_sentence": (
                    "This makes the approach trainable on sparse sensor data."
                ),
                "benefit_type": "mechanism_background",
                "query": "PINN residual loss background",
            }
        ],
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate(
            paper_id="fallback-paper",
            title="PINN Fallback Application",
            doi="10.1/fallback-app",
            abstract=(
                "A PINN application recovered boundary values from sparse "
                "sensor data."
            ),
        )

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-fallback",
    )

    applications = _read_ledger(tmp_path, "out-fallback")[
        "representative_applications"
    ]
    metrics = applications["metrics"]
    assert metrics["target_source"] == "deterministic_fallback"
    assert metrics["fallback_used"] is True
    assert metrics["targets_planned"] == 1
    assert metrics["examples_attached"] == 1
    assert any(
        "representative_application_fallback" in diagnostic
        for diagnostic in applications["diagnostics"]
    )
    assert report["status"] == "enhanced"


def test_application_local_first_no_s2_when_local_usable(tmp_path: Path) -> None:
    ledger = _ledger_for_fake_packet()
    caller, calls = _application_fake_caller(ledger)
    s2_called = {"value": False}

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate()

    def s2_search(query: str, max_results: int) -> list[dict]:
        s2_called["value"] = True
        return []

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        s2_search=s2_search,
    )

    assert s2_called["value"] is False
    ledger_data = _read_ledger(tmp_path)
    applications = ledger_data["representative_applications"]
    metrics = applications["metrics"]
    assert metrics["local_reuse_hits"] == 1
    assert metrics["s2_fallbacks"] == 0
    assert metrics["local_candidates_inspected"] == 1
    assert metrics["s2_candidates_inspected"] == 0
    assert metrics["examples_attached"] == 1
    assert metrics["application_writer_call_count"] == 1
    app_records = [
        record
        for record in ledger_data["records"]
        if "representative_application" in record.get("benefit_types", [])
    ]
    assert len(app_records) == 1
    assert app_records[0]["retrieval_origin"] == "local_metadata"
    assert app_records[0]["benefit_types"] == ["representative_application"]
    enhanced = _read_enhanced(tmp_path)
    assert "Representative implementation:" not in enhanced
    assert "One representative application used the method" in enhanced
    assert "[REF:doi:10.1/pinn-heat]" in enhanced
    assert "[REF:paper-s2-001]" in enhanced
    assert report["status"] == "enhanced"
    assert calls.count(
        "ChapterAssetRepresentativeApplicationWriterAgent"
    ) == 1


def test_application_local_request_bounded_to_four(tmp_path: Path) -> None:
    ledger = _ledger_for_fake_packet()
    caller, _calls = _application_fake_caller(ledger)
    local_requests: list[int] = []
    s2_called = {"value": False}

    def local_search(query: str, max_results: int) -> list[dict]:
        local_requests.append(max_results)
        return [
            {
                "paper_id": f"local-{index}",
                "title": "PINN Method Implementation Study",
                "abstract": (
                    "PINN method implementation for boundary recovery "
                    "from sparse sensor measurements."
                ),
                "relevance_score": 0.9,
            }
            for index in range(5)
        ]

    def s2_search(query: str, max_results: int) -> list[dict]:
        s2_called["value"] = True
        return []

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        s2_search=s2_search,
        name="out-local-bound",
        application_local_max_results=4,
        application_per_target_cap=4,
    )

    assert local_requests == [4]
    assert s2_called["value"] is False
    applications = _read_ledger(tmp_path, "out-local-bound")[
        "representative_applications"
    ]
    compliance = applications["metrics"]["per_target_cap_compliance"][0]
    assert compliance["local_requested"] == 4
    assert compliance["local_candidates_inspected"] == 5
    assert compliance["s2_requested"] == 0
    assert compliance["s2_cap_compliant"] is True
    assert report["status"] == "enhanced"


def test_application_s2_fallback_bounded_to_four_with_separate_counts(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, _calls = _application_fake_caller(ledger)
    s2_requests: list[int] = []

    def local_search(query: str, max_results: int) -> list[dict]:
        # Weak local overlap: not usable alone, not a clear mismatch.
        return [
            {
                "paper_id": "weak-local-1",
                "title": "Adaptive framework design",
                "abstract": (
                    "adaptive gradient neural network framework design "
                    "algorithm"
                ),
                "relevance_score": 0.5,
            }
        ]

    def s2_search(query: str, max_results: int) -> list[dict]:
        s2_requests.append(max_results)
        return [
            {
                "semantic_scholar_paper_id": f"s2-{index}",
                "title": "PINN Application Heat Transfer Study",
                "abstract": (
                    "PINN applied to heat transfer boundary recovery "
                    "with sparse sensor data."
                ),
                "relevance_score": 0.9,
            }
            for index in range(4)
        ]

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        s2_search=s2_search,
        name="out-s2",
        application_local_max_results=4,
        application_per_target_cap=4,
    )

    assert s2_requests == [4]
    applications = _read_ledger(tmp_path, "out-s2")[
        "representative_applications"
    ]
    metrics = applications["metrics"]
    assert metrics["s2_fallbacks"] == 1
    assert metrics["local_candidates_inspected"] == 1
    assert metrics["s2_candidates_inspected"] == 4
    assert metrics["examples_attached"] == 1
    compliance = metrics["per_target_cap_compliance"][0]
    assert compliance["s2_requested"] == 4
    assert compliance["s2_candidates_inspected"] == 4
    assert compliance["s2_cap_compliant"] is True
    app_records = [
        record
        for record in _read_ledger(tmp_path, "out-s2")["records"]
        if "representative_application" in record.get("benefit_types", [])
    ]
    assert app_records[0]["retrieval_origin"] == "semantic_scholar"
    assert report["status"] == "enhanced"


def test_application_low_threshold_accepts_mildly_relevant_material(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, _calls = _application_fake_caller(ledger)
    s2_called = {"value": False}

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate(
            paper_id="mild-local",
            title="PINN implementation note",
            doi="10.1/mild",
            abstract=(
                "A PINN implementation for heat conduction boundary "
                "recovery."
            ),
            relevance_score=0.3,
        )

    def s2_search(query: str, max_results: int) -> list[dict]:
        s2_called["value"] = True
        return []

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        s2_search=s2_search,
        name="out-mild",
    )

    assert s2_called["value"] is False
    applications = _read_ledger(tmp_path, "out-mild")[
        "representative_applications"
    ]
    assert applications["metrics"]["examples_attached"] == 1
    assert report["status"] == "enhanced"


def test_application_clear_mismatch_rejected_without_writer_call(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, calls = _application_fake_caller(ledger)
    s2_called = {"value": False}

    def local_search(query: str, max_results: int) -> list[dict]:
        return [
            {
                "paper_id": "chemistry-1",
                "title": "Organic Chemistry Synthesis",
                "abstract": (
                    "Organic chemical synthesis procedures unrelated to "
                    "neural networks."
                ),
                "relevance_score": 0.0,
            }
        ]

    def s2_search(query: str, max_results: int) -> list[dict]:
        s2_called["value"] = True
        return []

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        s2_search=s2_search,
        name="out-mismatch",
    )

    applications = _read_ledger(tmp_path, "out-mismatch")[
        "representative_applications"
    ]
    assert applications["records"] == []
    assert applications["metrics"]["skipped_targets"] == 1
    assert applications["metrics"]["application_writer_call_count"] == 0
    assert (
        "ChapterAssetRepresentativeApplicationWriterAgent" not in calls
    )
    assert any(
        "no_usable_application_example" in reason
        for reason in applications["metrics"]["stop_reasons"]
    )
    assert report["status"] == "enhanced"
    assert "One representative application" not in _read_enhanced(
        tmp_path, "out-mismatch"
    )


def test_application_missing_example_fails_open(tmp_path: Path) -> None:
    ledger = _ledger_for_fake_packet()
    caller, calls = _application_fake_caller(ledger)

    def local_search(query: str, max_results: int) -> list[dict]:
        return []

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-missing",
    )

    assert report["status"] == "enhanced"
    applications = _read_ledger(tmp_path, "out-missing")[
        "representative_applications"
    ]
    assert applications["records"] == []
    assert applications["metrics"]["skipped_targets"] == 1
    assert applications["metrics"]["application_writer_call_count"] == 0
    assert (
        "ChapterAssetRepresentativeApplicationWriterAgent" not in calls
    )
    assert any(
        "no_usable_application_example" in diagnostic
        for diagnostic in applications["diagnostics"]
    )


def test_application_writer_failure_fails_open(tmp_path: Path) -> None:
    ledger = _ledger_for_fake_packet()
    caller, calls = _application_fake_caller(
        ledger,
        writer_raise=True,
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate()

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-writer-fail",
        application_soft_min_targets=0,
    )

    assert report["status"] == "enhanced"
    applications = _read_ledger(tmp_path, "out-writer-fail")[
        "representative_applications"
    ]
    assert applications["records"] == []
    assert applications["metrics"]["application_writer_call_count"] == 1
    assert any(
        "application_writer_unavailable" in diagnostic
        for diagnostic in applications["diagnostics"]
    )
    assert calls.count(
        "ChapterAssetRepresentativeApplicationWriterAgent"
    ) == 1
    assert report["model_usage"]["call_count"] == 6


def test_application_writer_invented_number_rejected(tmp_path: Path) -> None:
    ledger = _ledger_for_fake_packet()
    caller, _calls = _application_fake_caller(
        ledger,
        writer_prose=(
            "One application reported a 99.9% efficiency gain in the "
            "demonstrated benchmark."
        ),
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate()

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-number",
    )

    applications = _read_ledger(tmp_path, "out-number")[
        "representative_applications"
    ]
    assert applications["records"] == []
    assert applications["metrics"]["skipped_targets"] == 1
    assert any(
        "invented_number" in reason
        for reason in applications["metrics"]["stop_reasons"]
    )
    assert report["status"] == "enhanced"
    assert "99.9%" not in _read_enhanced(tmp_path, "out-number")


def test_application_writer_raw_ref_marker_rejected(tmp_path: Path) -> None:
    ledger = _ledger_for_fake_packet()
    caller, _calls = _application_fake_caller(
        ledger,
        writer_prose=(
            "One study demonstrated the method [REF:doi:10.1/pinn-heat]."
        ),
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate()

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-ref",
    )

    applications = _read_ledger(tmp_path, "out-ref")[
        "representative_applications"
    ]
    assert applications["records"] == []
    assert any(
        "raw_ref_marker" in reason
        for reason in applications["metrics"]["stop_reasons"]
    )
    assert report["status"] == "enhanced"


def test_application_writer_single_batched_call_for_up_to_three_targets(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, calls = _application_fake_caller(
        ledger,
        block_count=3,
    )
    writer_payloads: list[dict] = []

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate(
            paper_id="app-paper",
            title="PINN Application Example",
            doi="10.1/app",
            abstract=(
                "PINN application for boundary recovery from sensor data."
            ),
        )

    def recording_caller(agent_name: str, messages: list[dict], **_kwargs):
        if agent_name == "ChapterAssetRepresentativeApplicationWriterAgent":
            writer_payloads.append(json.loads(messages[-1]["content"]))
        return caller(agent_name, messages, **_kwargs)

    report = _run_with(
        tmp_path,
        recording_caller,
        local_search=local_search,
        name="out-batch",
    )

    assert calls.count(
        "ChapterAssetRepresentativeApplicationWriterAgent"
    ) == 1
    assert len(writer_payloads) == 1
    assert len(writer_payloads[0]["targets"]) == 3
    applications = _read_ledger(tmp_path, "out-batch")[
        "representative_applications"
    ]
    assert applications["metrics"]["application_writer_call_count"] == 1
    assert applications["metrics"]["examples_attached"] == 3
    assert report["status"] == "enhanced"


def test_application_records_merged_into_canonical_records_and_deduped(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, _calls = _application_fake_caller(
        ledger,
        explanatory_rows=[
            {
                "block_index": 1,
                "target_sentence": (
                    "This makes the approach trainable on sparse sensor data."
                ),
                "benefit_type": "definition",
                "query": "PINN heat conduction application",
            }
        ],
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        # Same paper serves both the definition and the application.
        return _good_local_candidate(
            paper_id="shared-paper",
            title="Shared PINN Case Study",
            doi="10.1/shared",
            abstract=(
                "PINN application recovering boundary values from sparse "
                "sensor data."
            ),
        )

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-merge",
    )

    ledger_data = _read_ledger(tmp_path, "out-merge")
    assert len(ledger_data["records"]) == 1
    record = ledger_data["records"][0]
    assert set(record["benefit_types"]) == {
        "definition",
        "representative_application",
    }
    assert record["role"] == "explanatory_context"
    assert record["permission"] == "background_explanation_only"
    assert len(record["applications"]) == 1
    assert record["applications"][0]["benefit_type"] == (
        "representative_application"
    )
    assert record["applications"][0]["attached"] is True
    assert set(ledger_data["block_to_explanatory_handles"]) == {"1"}
    assert report["status"] == "enhanced"
    assert report["reference_metrics"]["explanatory_record_count"] == 1
    assert (
        report["reference_metrics"][
            "representative_application_record_count"
        ]
        == 1
    )


def test_application_disabled_is_noop_and_fail_open(tmp_path: Path) -> None:
    ledger = _ledger_for_fake_packet()
    caller, calls = _application_fake_caller(ledger)

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate()

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-disabled",
        representative_applications_enabled=False,
    )

    applications = _read_ledger(tmp_path, "out-disabled")[
        "representative_applications"
    ]
    assert applications["records"] == []
    assert applications["metrics"]["targets_planned"] == 0
    assert any(
        "representative_applications_disabled"
        in diagnostic
        for diagnostic in applications["diagnostics"]
    )
    assert report["status"] == "enhanced"
    assert (
        "ChapterAssetRepresentativeApplicationWriterAgent" not in calls
    )
    assert "One representative application" not in _read_enhanced(
        tmp_path, "out-disabled"
    )


def test_application_no_callbacks_skips_without_model_calls(
    tmp_path: Path,
) -> None:
    packet_path, old_path = _write_sources(tmp_path)
    ledger = _ledger_for_fake_packet()
    caller, calls = _application_fake_caller(ledger)
    report = enhancer.run_enhancement(
        packet_path=packet_path,
        old_draft_path=old_path,
        output_dir=tmp_path / "out-no-callbacks",
        live=True,
        qwen_caller=caller,
    )

    applications = _read_ledger(tmp_path, "out-no-callbacks")[
        "representative_applications"
    ]
    assert applications["records"] == []
    assert any(
        "representative_application_search_unavailable" in diagnostic
        for diagnostic in applications["diagnostics"]
    )
    assert report["status"] == "enhanced"
    assert (
        "ChapterAssetRepresentativeApplicationWriterAgent" not in calls
    )
    assert report["model_usage"]["call_count"] == 4


def test_application_target_cap_bounds_targets_per_chapter(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, _calls = _application_fake_caller(
        ledger,
        block_count=3,
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate(
            paper_id="cap-paper",
            title="PINN Application Example",
            doi="10.1/cap",
            abstract=(
                "PINN application for boundary recovery from sensor data."
            ),
        )

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-target-cap",
        application_max_targets=1,
    )

    applications = _read_ledger(tmp_path, "out-target-cap")[
        "representative_applications"
    ]
    assert applications["metrics"]["targets_planned"] == 1
    assert applications["metrics"]["examples_attached"] == 1
    assert report["status"] == "enhanced"


def test_application_fallback_requires_multi_sentence_block(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, _calls = _application_fake_caller(
        ledger,
        block_prose="A single sentence block is not a major block.",
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate()

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-short-block",
    )

    applications = _read_ledger(tmp_path, "out-short-block")[
        "representative_applications"
    ]
    assert applications["metrics"]["targets_planned"] == 0
    assert applications["metrics"]["examples_attached"] == 0
    assert "One representative application" not in _read_enhanced(
        tmp_path, "out-short-block"
    )
    assert report["status"] == "enhanced"


def test_application_metrics_are_complete_and_consistent(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, _calls = _application_fake_caller(ledger)

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate()

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-metrics",
        application_soft_min_targets=0,
    )

    metrics = _read_ledger(tmp_path, "out-metrics")[
        "representative_applications"
    ]["metrics"]
    required = {
        "targets_planned",
        "target_source",
        "fallback_used",
        "local_reuse_hits",
        "s2_fallbacks",
        "skipped_targets",
        "local_candidates_inspected",
        "s2_candidates_inspected",
        "examples_attached",
        "references_added_count",
        "application_writer_call_count",
        "per_target_cap_compliance",
        "stop_reasons",
    }
    assert required <= set(metrics)
    assert metrics["targets_planned"] == 1
    assert metrics["local_reuse_hits"] == 1
    assert metrics["s2_fallbacks"] == 0
    assert metrics["skipped_targets"] == 0
    assert metrics["local_candidates_inspected"] == 1
    assert metrics["s2_candidates_inspected"] == 0
    assert metrics["examples_attached"] == 1
    assert metrics["references_added_count"] == 1
    assert metrics["application_writer_call_count"] == 1
    assert len(metrics["per_target_cap_compliance"]) == 1
    assert report["representative_application_metrics"] == metrics
    assert report["model_usage"]["call_count"] == 6


def test_application_writer_never_invokes_reranker_or_duplicate_planner(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, calls = _application_fake_caller(
        ledger,
        block_count=2,
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate()

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-deterministic",
        explanatory_citations_enabled=False,
    )

    assert report["status"] == "enhanced"
    applications = _read_ledger(tmp_path, "out-deterministic")[
        "representative_applications"
    ]
    assert applications["metrics"]["examples_attached"] == 2
    assert "ChapterAssetExplanatoryCitationPlannerAgent" not in calls
    assert "ChapterAssetExplanatorySemanticRerankerAgent" not in calls
    assert calls.count(
        "ChapterAssetRepresentativeApplicationWriterAgent"
    ) == 1


def test_application_prose_validation_unit_rules() -> None:
    target = {
        "handle": "A01",
        "block_index": 1,
        "anchor_sentence": "The method is trainable.",
        "query": "PINN application",
    }
    candidate = {
        "stable_paper_id": "doi:10.1/pinn-heat",
        "marker_id": "doi:10.1/pinn-heat",
        "metadata": {
            "title": "PINN Heat Conduction Case Study",
            "year": "2024",
            "paper_id": "app-local-1",
            "abstract": (
                "A PINN implementation recovered heat conduction boundary "
                "values from sparse sensor data."
            ),
        },
    }
    valid, reason = enhancer._validate_application_prose(
        "One study recovered boundary values from sparse sensor data.",
        target=target,
        candidate=candidate,
    )
    assert valid is True
    assert reason == ""
    # Invented number is rejected even when the prose is otherwise natural.
    valid, reason = enhancer._validate_application_prose(
        "One study reported a 99.9% efficiency gain.",
        target=target,
        candidate=candidate,
    )
    assert valid is False
    assert reason == "invented_number"
    # Source-reported year is acceptable because it is part of the payload.
    valid, _ = enhancer._validate_application_prose(
        "The 2024 study recovered boundary values from sparse data.",
        target=target,
        candidate=candidate,
    )
    assert valid is True
    # Raw citation markers and identifier leaks are rejected.
    valid, reason = enhancer._validate_application_prose(
        "One study recovered values [REF:doi:10.1/pinn-heat].",
        target=target,
        candidate=candidate,
    )
    assert valid is False
    assert reason == "raw_ref_marker"
    valid, reason = enhancer._validate_application_prose(
        "One study A01 recovered boundary values.",
        target=target,
        candidate=candidate,
    )
    assert valid is False
    assert reason == "raw_identifier_leak"
    # More than three sentences is unsupported and skipped.
    valid, reason = enhancer._validate_application_prose(
        "One study recovered values. It used sparse sensors. "
        "It reported a benefit. A fourth sentence exceeds the limit.",
        target=target,
        candidate=candidate,
    )
    assert valid is False
    assert reason == "too_many_sentences"


def _three_sentence_prose() -> str:
    return (
        "The PINN method embeds governing equations as residual losses. "
        "This makes the approach trainable on sparse sensor data. "
        "A second application setting recovered boundary values in practice."
    )


def test_application_planner_payload_carries_target_budget_policy(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, _calls = _application_fake_caller(
        ledger,
        block_count=2,
        block_prose=[
            _three_sentence_prose(),
            _three_sentence_prose(),
        ],
        explanatory_rows=[
            {
                "sentence_handle": "B01-S01",
                "benefit_type": "representative_application",
                "query": "PINN heat conduction application one",
            },
            {
                "sentence_handle": "B01-S02",
                "benefit_type": "representative_application",
                "query": "PINN heat conduction application two",
            },
        ],
        supplemental_explanatory_rows=[
            {
                "sentence_handle": "B01-S03",
                "benefit_type": "representative_application",
                "query": "PINN heat conduction application three",
            },
            {
                "sentence_handle": "B02-S01",
                "benefit_type": "representative_application",
                "query": "PINN heat conduction application four",
            },
            {
                "sentence_handle": "B02-S02",
                "benefit_type": "representative_application",
                "query": "PINN heat conduction application five",
            },
        ],
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate()

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-policy",
        application_max_targets=8,
        application_soft_min_targets=5,
    )

    initial_payload = caller.planner_payloads[0]
    assert initial_payload["representative_application_policy"] == {
        "max_targets": 8,
        "soft_min_targets": 5,
        "mode": "initial",
    }
    assert "actively propose" in initial_payload["instruction"]
    assert caller.planner_modes == [
        "initial",
        "supplemental_representative_applications",
    ]
    supplemental_payload = caller.planner_payloads[1]
    assert supplemental_payload["representative_application_policy"][
        "mode"
    ] == "supplemental_representative_applications"
    remaining_handles = {
        row["sentence_handle"] for row in supplemental_payload["sentence_table"]
    }
    # Only remaining sentences reach the supplemental pass.
    assert "B01-S03" in remaining_handles
    assert "B01-S01" not in remaining_handles

    applications = _read_ledger(tmp_path, "out-policy")[
        "representative_applications"
    ]
    metrics = applications["metrics"]
    assert metrics["raw_planner_application_row_count"] == 5
    assert metrics["accepted_distinct_application_target_count"] == 5
    assert metrics["application_target_shortfall_vs_soft_min"] == 0
    assert metrics["application_target_expansion_call_count"] == 1
    assert metrics["application_target_expansion_reason"] == (
        "shortfall_reduced"
    )
    assert metrics["targets_planned"] == 5
    assert metrics["examples_attached"] == 5
    assert report["status"] == "enhanced"


def test_application_planner_rows_preserved_up_to_max_without_shortfall(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, _calls = _application_fake_caller(
        ledger,
        block_count=2,
        block_prose=[
            _three_sentence_prose(),
            _three_sentence_prose(),
        ],
        explanatory_rows=[
            {
                "sentence_handle": f"B0{block}-S{sentence:02d}",
                "benefit_type": "representative_application",
                "query": f"application query {block}-{sentence}",
            }
            for block, sentence in (
                (1, 1),
                (1, 2),
                (1, 3),
                (2, 1),
                (2, 2),
            )
        ],
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate()

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-preserved",
        application_max_targets=8,
        application_soft_min_targets=4,
    )

    applications = _read_ledger(tmp_path, "out-preserved")[
        "representative_applications"
    ]
    metrics = applications["metrics"]
    assert metrics["raw_planner_application_row_count"] == 5
    assert metrics["accepted_distinct_application_target_count"] == 5
    assert metrics["application_target_shortfall_vs_soft_min"] == 0
    assert metrics["application_target_expansion_call_count"] == 0
    assert metrics["application_target_expansion_reason"] == "no_shortfall"
    assert metrics["targets_planned"] == 5
    assert metrics["examples_attached"] == 5
    assert caller.planner_modes == ["initial"]
    assert report["status"] == "enhanced"


def test_application_planner_rows_deduplicated_before_delegation(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, _calls = _application_fake_caller(
        ledger,
        block_count=1,
        block_prose=_three_sentence_prose(),
        explanatory_rows=[
            {
                "sentence_handle": "B01-S01",
                "benefit_type": "representative_application",
                "query": "duplicate query",
            },
            {
                "sentence_handle": "B01-S01",
                "benefit_type": "representative_application",
                "query": "duplicate query again",
            },
            {
                "sentence_handle": "B01-S02",
                "benefit_type": "representative_application",
                "query": "distinct query",
            },
        ],
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate()

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-dedup",
        application_max_targets=8,
        application_soft_min_targets=0,
    )

    applications = _read_ledger(tmp_path, "out-dedup")[
        "representative_applications"
    ]
    metrics = applications["metrics"]
    assert metrics["raw_planner_application_row_count"] == 3
    assert metrics["accepted_distinct_application_target_count"] == 2
    assert metrics["targets_planned"] == 2
    assert metrics["examples_attached"] == 2
    assert metrics["application_target_expansion_call_count"] == 0
    assert report["status"] == "enhanced"


def test_application_supplemental_call_capped_at_one_and_never_repeated(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, _calls = _application_fake_caller(
        ledger,
        block_count=1,
        block_prose=_three_sentence_prose(),
        explanatory_rows=[
            {
                "sentence_handle": "B01-S01",
                "benefit_type": "representative_application",
                "query": "first application",
            }
        ],
        supplemental_explanatory_rows=[
            {
                "sentence_handle": "B01-S02",
                "benefit_type": "representative_application",
                "query": "second application",
            },
            {
                "sentence_handle": "B01-S03",
                "benefit_type": "representative_application",
                "query": "third application",
            },
        ],
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate()

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-one-supplemental",
        application_max_targets=8,
        application_soft_min_targets=4,
    )

    applications = _read_ledger(tmp_path, "out-one-supplemental")[
        "representative_applications"
    ]
    metrics = applications["metrics"]
    # One bounded supplemental call; still below the soft minimum, but the
    # shortfall is advisory and never triggers a second call.
    assert caller.planner_modes == [
        "initial",
        "supplemental_representative_applications",
    ]
    assert metrics["application_target_expansion_call_count"] == 1
    assert metrics["application_target_expansion_reason"] == (
        "shortfall_reduced"
    )
    assert metrics["raw_planner_application_row_count"] == 3
    assert metrics["accepted_distinct_application_target_count"] == 3
    assert metrics["application_target_shortfall_vs_soft_min"] == 1
    assert metrics["targets_planned"] == 3
    assert metrics["examples_attached"] == 3
    assert report["status"] == "enhanced"


def test_application_no_supplemental_call_when_no_remaining_sentences(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, _calls = _application_fake_caller(
        ledger,
        block_count=1,
        block_prose=_three_sentence_prose(),
        explanatory_rows=[
            {
                "sentence_handle": "B01-S01",
                "benefit_type": "representative_application",
                "query": "application one",
            },
            {
                "sentence_handle": "B01-S02",
                "benefit_type": "representative_application",
                "query": "application two",
            },
            {
                "sentence_handle": "B01-S03",
                "benefit_type": "representative_application",
                "query": "application three",
            },
        ],
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate()

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-no-remaining",
        application_max_targets=8,
        application_soft_min_targets=4,
    )

    ledger_data = _read_ledger(tmp_path, "out-no-remaining")
    applications = ledger_data["representative_applications"]
    metrics = applications["metrics"]
    assert caller.planner_modes == ["initial"]
    assert metrics["application_target_expansion_call_count"] == 0
    assert metrics["application_target_expansion_reason"] == (
        "no_eligible_remaining_sentences"
    )
    assert metrics["raw_planner_application_row_count"] == 3
    assert metrics["application_target_shortfall_vs_soft_min"] == 1
    assert metrics["targets_planned"] == 3
    assert report["status"] == "enhanced"
    assert any(
        "application_target_expansion_skipped: "
        "no_eligible_remaining_sentences" in diagnostic
        for diagnostic in ledger_data["diagnostics"]
    )


def test_application_supplemental_planner_failure_fails_open(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, _calls = _application_fake_caller(
        ledger,
        block_count=1,
        block_prose=_three_sentence_prose(),
        explanatory_rows=[
            {
                "sentence_handle": "B01-S01",
                "benefit_type": "representative_application",
                "query": "application one",
            }
        ],
        supplemental_raise=True,
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate()

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-supplemental-fail",
        application_max_targets=8,
        application_soft_min_targets=4,
    )

    ledger_data = _read_ledger(tmp_path, "out-supplemental-fail")
    applications = ledger_data["representative_applications"]
    metrics = applications["metrics"]
    assert metrics["application_target_expansion_call_count"] == 1
    assert metrics["application_target_expansion_reason"] == (
        "supplemental_unavailable"
    )
    assert any(
        "application_target_expansion_unavailable" in diagnostic
        for diagnostic in ledger_data["diagnostics"]
    )
    assert metrics["targets_planned"] == 1
    assert report["status"] == "enhanced"


def test_application_default_settings_trigger_one_bounded_expansion(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, _calls = _application_fake_caller(
        ledger,
        block_count=1,
        block_prose=_three_sentence_prose(),
        explanatory_rows=[
            {
                "sentence_handle": "B01-S01",
                "benefit_type": "representative_application",
                "query": "application one",
            }
        ],
        supplemental_explanatory_rows=[
            {
                "sentence_handle": "B01-S02",
                "benefit_type": "representative_application",
                "query": "application two",
            },
            {
                "sentence_handle": "B01-S03",
                "benefit_type": "representative_application",
                "query": "application three",
            },
        ],
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate()

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-defaults",
    )

    applications = _read_ledger(tmp_path, "out-defaults")[
        "representative_applications"
    ]
    metrics = applications["metrics"]
    assert caller.planner_modes == [
        "initial",
        "supplemental_representative_applications",
    ]
    assert metrics["application_target_expansion_call_count"] == 1
    assert metrics["application_target_expansion_reason"] == (
        "shortfall_reduced"
    )
    assert metrics["application_target_shortfall_vs_soft_min"] == 1
    assert metrics["targets_planned"] == 3
    assert metrics["examples_attached"] == 3
    assert report["status"] == "enhanced"


def test_cli_exposes_application_budget_controls() -> None:
    parser = cli.build_arg_parser()
    args = parser.parse_args(
        [
            "--packet-path",
            "packet.json",
            "--old-draft",
            "old.md",
            "--output-dir",
            "out",
            "--application-max-targets",
            "8",
            "--application-soft-min-targets",
            "5",
            "--application-per-target-cap",
            "4",
            "--application-local-max-results",
            "4",
            "--application-writer-tier",
            "c2_model",
        ]
    )
    assert args.application_max_targets == 8
    assert args.application_soft_min_targets == 5
    assert args.application_per_target_cap == 4
    assert args.application_local_max_results == 4
    assert args.application_writer_tier == "c2_model"
    assert args.disable_representative_applications is False

    defaults = parser.parse_args(
        [
            "--packet-path",
            "packet.json",
            "--old-draft",
            "old.md",
            "--output-dir",
            "out",
        ]
    )
    assert defaults.application_max_targets == (
        enhancer.DEFAULT_APPLICATION_MAX_TARGETS
    )
    assert defaults.application_soft_min_targets == (
        enhancer.DEFAULT_APPLICATION_SOFT_MIN_TARGETS
    )
    assert defaults.application_per_target_cap == (
        enhancer.DEFAULT_APPLICATION_PER_TARGET_CAP
    )
    assert defaults.application_local_max_results == (
        enhancer.DEFAULT_APPLICATION_LOCAL_MAX_RESULTS
    )


def test_application_duplicate_rows_do_not_suppress_supplemental_shortfall(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, _calls = _application_fake_caller(
        ledger,
        block_count=1,
        block_prose=_three_sentence_prose(),
        explanatory_rows=[
            {
                "sentence_handle": "B01-S01",
                "benefit_type": "representative_application",
                "query": "duplicate one",
            },
            {
                "sentence_handle": "B01-S01",
                "benefit_type": "representative_application",
                "query": "duplicate two",
            },
            {
                "sentence_handle": "B01-S01",
                "benefit_type": "representative_application",
                "query": "duplicate three",
            },
            {
                "sentence_handle": "B01-S02",
                "benefit_type": "representative_application",
                "query": "distinct second",
            },
        ],
        supplemental_explanatory_rows=[
            {
                "sentence_handle": "B01-S03",
                "benefit_type": "representative_application",
                "query": "remaining third",
            }
        ],
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate()

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-dup-shortfall",
        application_max_targets=8,
        application_soft_min_targets=4,
    )

    ledger_data = _read_ledger(tmp_path, "out-dup-shortfall")
    expansion = ledger_data["application_target_expansion"]
    # Raw rows are high (4), but distinct targets are only 2, so the
    # supplemental pass must still fire.
    assert expansion["initial_application_row_count"] == 4
    assert expansion["initial_distinct_target_count"] == 2
    assert expansion["call_count"] == 1
    assert expansion["reason"] == "shortfall_reduced"
    assert expansion["supplemental_application_row_count"] == 1
    assert expansion["supplemental_distinct_target_count"] == 1
    assert expansion["shortfall_vs_soft_min"] == 1

    applications = ledger_data["representative_applications"]
    metrics = applications["metrics"]
    assert metrics["raw_planner_application_row_count"] == 5
    assert metrics["accepted_distinct_application_target_count"] == 3
    assert metrics["application_target_shortfall_vs_soft_min"] == 1
    assert metrics["targets_planned"] == 3
    assert metrics["examples_attached"] == 3
    assert report["status"] == "enhanced"


def test_application_writer_batches_five_targets_into_two_calls(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, calls = _application_fake_caller(
        ledger,
        block_count=2,
        block_prose=[
            _three_sentence_prose(),
            _three_sentence_prose(),
        ],
        explanatory_rows=[
            {
                "sentence_handle": f"B0{block}-S{sentence:02d}",
                "benefit_type": "representative_application",
                "query": f"application {block}-{sentence}",
            }
            for block, sentence in (
                (1, 1),
                (1, 2),
                (1, 3),
                (2, 1),
                (2, 2),
            )
        ],
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate()

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-batches",
        application_max_targets=8,
        application_soft_min_targets=5,
    )

    assert calls.count(
        "ChapterAssetRepresentativeApplicationWriterAgent"
    ) == 2
    assert len(caller.writer_payloads) == 2
    assert [len(payload["targets"]) for payload in caller.writer_payloads] == [
        3,
        2,
    ]
    assert caller.writer_payloads[0]["batch"] == {
        "index": 1,
        "size": 3,
        "total_batches": 2,
    }
    assert caller.writer_payloads[1]["batch"] == {
        "index": 2,
        "size": 2,
        "total_batches": 2,
    }
    applications = _read_ledger(tmp_path, "out-batches")[
        "representative_applications"
    ]
    metrics = applications["metrics"]
    assert metrics["application_writer_call_count"] == 2
    assert metrics["examples_attached"] == 5
    assert report["model_usage"]["call_count"] == 8
    assert report["status"] == "enhanced"


def test_application_writer_partial_batch_failure_fails_open(
    tmp_path: Path,
) -> None:
    ledger = _ledger_for_fake_packet()
    caller, _calls = _application_fake_caller(
        ledger,
        block_count=2,
        block_prose=[
            _three_sentence_prose(),
            _three_sentence_prose(),
        ],
        explanatory_rows=[
            {
                "sentence_handle": f"B0{block}-S{sentence:02d}",
                "benefit_type": "representative_application",
                "query": f"application {block}-{sentence}",
            }
            for block in (1, 2)
            for sentence in (1, 2, 3)
        ],
        writer_raise_on_batch=2,
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate()

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-partial-batch",
        application_max_targets=8,
        application_soft_min_targets=6,
    )

    applications = _read_ledger(tmp_path, "out-partial-batch")[
        "representative_applications"
    ]
    metrics = applications["metrics"]
    # Two batches attempted; the second failed, the first three examples stay.
    assert metrics["application_writer_call_count"] == 2
    assert metrics["examples_attached"] == 3
    assert metrics["skipped_targets"] == 3
    assert len(caller.writer_payloads) == 2
    assert any(
        "application_writer_unavailable" in diagnostic
        for diagnostic in applications["diagnostics"]
    )
    enhanced = _read_enhanced(tmp_path, "out-partial-batch")
    assert (
        "One representative application used the method"
        in enhanced
    )
    assert report["status"] == "enhanced"


def test_application_payload_default_max_matches_enhancer_default() -> None:
    blocks = [
        {
            "block_index": 1,
            "title": "Block",
            "goal": "Explain the method.",
            "prose": (
                "The method embeds governing equations. "
                "It is trainable on sparse data."
            ),
        }
    ]
    payload = enhancer._explanatory_payload(
        blocks=blocks,
        plan={"section_id": "S01"},
        title="PINNs",
    )
    assert payload["representative_application_policy"]["max_targets"] == (
        enhancer.DEFAULT_APPLICATION_MAX_TARGETS
    )
    assert payload["representative_application_policy"][
        "soft_min_targets"
    ] == enhancer.DEFAULT_APPLICATION_SOFT_MIN_TARGETS


def test_application_expansion_cap_constant_guards_supplemental(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(enhancer, "_APPLICATION_EXPANSION_MAX_CALLS", 0)
    ledger = _ledger_for_fake_packet()
    caller, _calls = _application_fake_caller(
        ledger,
        block_count=1,
        block_prose=_three_sentence_prose(),
        explanatory_rows=[
            {
                "sentence_handle": "B01-S01",
                "benefit_type": "representative_application",
                "query": "application one",
            }
        ],
        supplemental_explanatory_rows=[
            {
                "sentence_handle": "B01-S02",
                "benefit_type": "representative_application",
                "query": "application two",
            }
        ],
    )

    def local_search(query: str, max_results: int) -> list[dict]:
        return _good_local_candidate()

    report = _run_with(
        tmp_path,
        caller,
        local_search=local_search,
        name="out-cap-guard",
        application_max_targets=8,
        application_soft_min_targets=4,
    )

    ledger_data = _read_ledger(tmp_path, "out-cap-guard")
    expansion = ledger_data["application_target_expansion"]
    assert expansion["call_count"] == 0
    assert expansion["reason"] == "expansion_cap_reached"
    assert caller.planner_modes == ["initial"]
    assert any(
        "expansion_cap_reached" in diagnostic
        for diagnostic in ledger_data["diagnostics"]
    )
    applications = ledger_data["representative_applications"]
    assert applications["metrics"]["application_target_expansion_reason"] == (
        "expansion_cap_reached"
    )
    assert report["status"] == "enhanced"
