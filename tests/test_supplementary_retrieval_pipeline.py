"""Offline tests for the supplementary retrieval production adapter."""

from __future__ import annotations

import json
import math
import re
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

from optomind_research.runtime.supplementary_query_dedup import (
    AmbiguousGroup,
    KnownQuery,
    QueryCandidate,
)
from optomind_research.runtime.supplementary_retrieval_contract import (
    DEFAULT_PORTFOLIO_LIMITS,
    GAP_TYPE_REQUIRED_CONTEXT_FIELDS,
    GAP_TYPES,
    ContextRegistry,
    SupplementaryRetrievalTask,
    project_context_for_task,
)
from optomind_research.runtime.supplementary_retrieval_pipeline import (
    DEFAULT_ADJUDICATOR_PROMPT,
    DEFAULT_GENERATOR_PROMPT,
    PipelineUsage,
    QueryGenerationError,
    QueryGenerationResult,
    SupplementarySemanticEngine,
    SupplementaryRetrievalPipeline,
    VisualPipelineUnsupportedError,
    _apply_search_background_cue,
    _records_from_named_cells,
    _records_from_query_strings,
    build_supplementary_query_plan,
    finalize_task_material_cache,
    make_literature_materialize_callback,
    make_literature_retrieve_callback,
    make_qwen_adjudicator,
    make_qwen_query_generator,
)
from optomind_research.runtime.supplementary_retrieval_service import (
    STATUS_COMMITTED,
    STATUS_FAILED,
    STATUS_NO_PROGRESS,
    MaterializationOutcome,
    RetrievalOutcome,
    ROUTE_LITERATURE,
    ServiceCallbacks,
    SupplementaryRetrievalService,
)
from optomind_research.s2_discovery import (
    S2DiscoveryPortfolioBuilder,
    ScholarFacetRequest,
)
from optomind_research.s2_intelligence_gateway import S2GatewayResponse
from optomind_research.runtime.topic_scoped_kb_stage import (
    _scope_match,
    derive_topic_scope_contract,
)


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory (pytest's default creates ACL-blocked dirs)."""
    base = Path(tempfile.gettempdir()) / "optomind-supplementary-tmp"
    base.mkdir(exist_ok=True)
    path = base / f"{request.node.name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def _registry() -> ContextRegistry:
    registry = ContextRegistry()
    registry.set("user_question", "How do radiative cooling multilayers compare?")
    registry.set(
        "dynamic_axes",
        [{"axis_id": "Q01", "description": "multilayer mechanism"}],
    )
    registry.set(
        "section_task",
        {"section_id": "S01", "title": "Mechanism", "task": "Explain physics."},
    )
    registry.set(
        "target_claim_or_sentence",
        {"claim_id": "C1", "statement": "Cooling power exceeds 60 W/m2."},
    )
    registry.set("argument_role", "mechanism_explanation")
    registry.set(
        "bound_papers_and_quotes",
        [{"paper_id": "p1", "quote": "emissivity 0.95"}],
    )
    registry.set("reviewer_feedback", {"mentor": "needs direct measurement support"})
    registry.set("author_revision_history", [{"revision": 1, "outcome": "still_open"}])
    registry.set("missing_fact_units", ["cooling_power_measured"])
    registry.set(
        "required_material_strength",
        {"minimum": "factual_support", "abstract_ceiling": "background_only"},
    )
    registry.set("retrieval_success_criteria", ["has_measured_cooling_power"])
    registry.set("existing_paper_identities", ["doi:10.1/example"])
    registry.set("historical_queries", [])
    registry.set("concurrent_queries", [])
    registry.set(
        "current_review_structure",
        {
            "existing_sections": [{"section_id": "S01"}],
            "new_sections": [],
            "new_subsections_per_existing_section": {},
        },
    )
    registry.set(
        "paper_introduction_conclusion_excerpts",
        {
            "current_paper_introduction_excerpt": "Radiative cooling is emerging.",
            "current_paper_conclusion_excerpt": "Fabrication challenges remain.",
        },
    )
    registry.set(
        "whole_review_feedback",
        {"section_count": 8, "uncovered_roles": ["boundary"]},
    )
    registry.set(
        "visual_slots",
        [{"slot_id": "V01", "role": "mechanism_anchor", "section_id": "S01"}],
    )
    registry.set("visual_gaps", ["mechanism_anchor_figure_missing"])
    registry.set(
        "topic_scope",
        {
            "topic": "radiative cooling multilayers",
            "main_scope": "optical multilayer radiative cooling",
            "lenses": ["mechanism", "fabrication"],
            "inclusion_boundaries": ["optical multilayer films"],
            "exclusion_boundaries": ["acoustic metalens"],
            "scope_items": ["group delay engineering"],
        },
    )
    registry.set(
        "materialization_policy",
        {
            "priority": ["s2_structured_body", "public_oa_fulltext", "abstract_claim"],
            "abstract_background_only": True,
        },
    )
    registry.set("portfolio_limits", dict(DEFAULT_PORTFOLIO_LIMITS))
    return registry.freeze()


def _task(
    gap_type: str = "claim_evidence_gap",
    *,
    task_id: str = "task-1",
    queries: tuple[str, ...] = ("radiative cooling multilayer inverse design",),
    **kwargs,
) -> SupplementaryRetrievalTask:
    from optomind_research.runtime.supplementary_retrieval_contract import (
        GAP_TYPE_REQUIRED_CONTEXT_FIELDS,
    )

    kwargs.setdefault(
        "source_provenance", {"producer": "test", "stage": "pipeline"}
    )
    kwargs.setdefault("success_criteria", ("has_adequate_evidence",))
    kwargs.setdefault("material_requirements", ("s2_structured_body",))
    kwargs.setdefault("retrieval_queries", queries)
    kwargs.setdefault("visual_route", gap_type == "visual_material_gap")
    return SupplementaryRetrievalTask(
        task_id=task_id,
        gap_type=gap_type,
        context_refs=GAP_TYPE_REQUIRED_CONTEXT_FIELDS[gap_type],
        priority=1,
        **kwargs,
    )


def _fake_prepare(calls: list, *, material_status: str = "s2_body", admitted: int = 1):
    def prepare(**kwargs):
        calls.append(kwargs)
        work_dir = Path(kwargs["work_dir"])
        ledger = {
            "schema_version": "optomind.s2_material_flow.v1",
            "summary": {"paper_count": admitted, "admitted_paper_count": admitted},
            "papers": [
                {
                    "paper_id": f"p{index}",
                    "title": f"Paper {index}",
                    "doi": f"10.1/p{index}",
                    "material_status": material_status,
                    "admitted_to_downstream": True,
                }
                for index in range(1, admitted + 1)
            ],
        }
        (work_dir / "S2_MATERIAL_FLOW_LEDGER.json").write_text(
            json.dumps(ledger), encoding="utf-8"
        )
        report = {
            "schema_version": "review_harness.s2_bootstrap.v3",
            "status": "completed",
            "search_queries": ["supplementary query"],
            "runtime_kb_sqlite": str(work_dir / "review_knowledge_base.s2.sqlite"),
            "graph_path": str(work_dir / "S2_LITERATURE_GRAPH.json"),
            "material_flow_ledger_path": str(work_dir / "S2_MATERIAL_FLOW_LEDGER.json"),
            "telemetry_path": str(work_dir / "S2_QUERY_TELEMETRY.json"),
            "external_query_runs": [{"query": "supplementary query", "status_code": 200}],
            "s2_query_telemetry": {"schema_version": "x", "graph_searches": 1},
            "material_flow_summary": {"admitted_paper_count": admitted},
            "report_sha256": "fake-report-sha",
            "reused": False,
        }
        (work_dir / "S2_BOOTSTRAP_REPORT.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        (work_dir / "review_knowledge_base.s2.sqlite").write_bytes(b"fake-kb")
        (work_dir / "S2_QUERY_TELEMETRY.json").write_text("{}", encoding="utf-8")
        return report

    return prepare


def _fake_create_empty_kb(calls: list):
    def create_empty_kb(path):
        calls.append(str(path))
        Path(path).write_bytes(b"empty-seed")
        return Path(path)

    return create_empty_kb


def _fake_build_packets(calls: list, *, material_status: str = "s2_body"):
    def build_packets(**kwargs):
        calls.append(kwargs)
        return {
            "question": "How do radiative cooling multilayers compare?",
            "packets": [
                {
                    "canonical_work_id": "work:abc",
                    "material_classes": [material_status],
                    "question": "How do radiative cooling multilayers compare?",
                }
            ],
            "source_material_record_count": 1,
        }

    return build_packets


def _fake_extract_cards(calls: list, *, usage=None):
    def extract_cards(**kwargs):
        calls.append(kwargs)
        cards_dir = Path(kwargs["output_dir"]) / "cards"
        cards_dir.mkdir(parents=True, exist_ok=True)
        llm_usage = usage or {
            "input_tokens": 100,
            "output_tokens": 50,
            "model_tier": "b_plus_model",
            "model_name": "qwen3.7-flash",
        }
        (cards_dir / "work_abc.json").write_text(
            json.dumps(
                {
                    "card": {
                        "canonical_work_id": "work:abc",
                        "query_annotation": {
                            "model_version": "qwen3.7-flash",
                            "question_hash": "sha256:abc",
                        },
                        "seed_axis_assignments": [],
                        "emergent_axis_candidates": [],
                        "propositions": [
                            {
                                "proposition_id": "p1",
                                "statement": "measured cooling power",
                                "evidence_chunk_ids": ["c1"],
                                "question_function": "support",
                            }
                        ],
                        "background_contexts": [],
                    },
                    "audit": {"llm_usage": llm_usage},
                }
            ),
            encoding="utf-8",
        )
        return {
            "rows": [{"status": "passed", "llm_usage": llm_usage}],
            "selected_work_count": 1,
            "new_attempt_count": 1,
            "reused_count": 0,
            "successful_card_count": 1,
            "failed_count": 0,
        }

    return extract_cards


def _fake_build_units(calls: list, *, content_depth: str = "fulltext"):
    def build_units(**kwargs):
        calls.append(kwargs)
        return {
            "units": [
                {
                    "unit_id": "unit:text:abc",
                    "identity": {"chunk_id": "c1", "title": "Paper 1"},
                    "durable_content": {
                        "content_depth": content_depth,
                        "content_hash": "sha256:h1",
                        "normalized_text": "measured result text",
                    },
                    "durable_content_card": {"observable_content": "result"},
                }
            ]
        }

    return build_units


def _fake_embedder(calls: list):
    def embedder(texts, *, usage_accumulator=None, **kwargs):
        calls.append(list(texts))
        if usage_accumulator is not None:
            usage_accumulator["input_tokens"] = (
                usage_accumulator.get("input_tokens", 0) + 10 * len(texts)
            )
            usage_accumulator["request_count"] = (
                usage_accumulator.get("request_count", 0) + 1
            )
        return [[1.0, 0.0, 0.0] for _ in texts]

    return embedder


def _semantic_fake_embedder(calls: list):
    """Term-unit vector embedder: domain tokens are semantically closer."""

    def embedder(texts, *, usage_accumulator=None, **kwargs):
        calls.append(list(texts))
        tokens = {
            "optical": (1, 0, 0, 0, 0, 0, 0, 0),
            "electromagnetic": (0, 1, 0, 0, 0, 0, 0, 0),
            "pinn": (0, 0, 1, 0, 0, 0, 0, 0),
            "euv": (0, 0, 0, 1, 0, 0, 0, 0),
            "lithography": (0, 0, 0, 1, 0, 0, 0, 0),
            "near": (0, 0, 0, 0, 1, 0, 0, 0),
            "field": (0, 0, 0, 0, 1, 0, 0, 0),
            "error": (0, 0, 0, 0, 1, 0, 0, 0),
            "truncation": (0, 0, 0, 0, 1, 0, 0, 0),
            "evanescent": (0, 0, 0, 0, 1, 0, 0, 0),
            "multilayer": (1, 0, 0, 0, 0, 1, 0, 0),
            "radiative": (0, 0, 0, 0, 0, 1, 0, 0),
            "cooling": (0, 0, 0, 0, 0, 1, 0, 0),
            "underwater": (0, 0, 0, 0, 0, 0, 1, 0),
            "acoustic": (0, 0, 0, 0, 0, 0, 1, 0),
            "stock": (0, 0, 0, 0, 0, 0, 0, 1),
            "market": (0, 0, 0, 0, 0, 0, 0, 1),
            "sodium": (0, 0, 0, 0, 0, 0, 1, 1),
            "channel": (0, 0, 0, 0, 0, 0, 1, 1),
            "kinetics": (0, 0, 0, 0, 0, 0, 1, 1),
            "alignment": (0, 0, 0, 0, 0, 0, 0, 0, 1, 0),
            "positioning": (0, 0, 0, 0, 0, 0, 0, 0, 1, 0),
            "tolerance": (0, 0, 0, 0, 0, 0, 0, 0, 1, 0),
            "fabrication": (0, 0, 0, 0, 0, 0, 0, 0, 0, 1),
        }
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * 10
            for word in re.findall(
                r"[a-z0-9]{3,}", str(text or "").casefold()
            ):
                if word in tokens:
                    for index, value in enumerate(tokens[word]):
                        vector[index] += value
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        if usage_accumulator is not None:
            usage_accumulator["input_tokens"] = (
                usage_accumulator.get("input_tokens", 0)
                + sum(len(str(text or "").split()) for text in texts)
            )
            usage_accumulator["request_count"] = (
                usage_accumulator.get("request_count", 0) + 1
            )
        return vectors

    return embedder


def _fake_qwen(content: str, calls: list | None = None, usage: dict | None = None):
    def qwen(*args, **kwargs):
        if calls is not None:
            calls.append(kwargs)
        return {
            "content": content,
            "_llm_usage": usage
            or {
                "input_tokens": 11,
                "output_tokens": 7,
                "model_tier": "b_plus_model",
                "model_name": "qwen3.7-flash",
            },
        }

    return qwen


def _execution_meta(
    idempotency_key: str = "supplementary:abc",
    attempt_id: str = "attempt:1",
) -> dict:
    return {
        "schema_version": "supplementary_retrieval.execution_meta.v1",
        "idempotency_key": idempotency_key,
        "task_fingerprint": "fp-1",
        "task_id": "task-1",
        "attempt_id": attempt_id,
        "route": ROUTE_LITERATURE,
        "gap_type": "claim_evidence_gap",
    }


def test_query_plan_adapter_preserves_identity_queries_and_exclusions() -> None:
    registry = _registry()
    task = _task(task_id="task-plan")
    resolved = registry.resolve(task.context_refs)
    records = [
        {
            "query_id": "q1",
            "text": "radiative cooling multilayer inverse design optimization",
            "decision": "keep",
            "reasons": [],
        }
    ]
    plan = build_supplementary_query_plan(task, resolved, records)
    assert plan["input"]["user_query"] == "How do radiative cooling multilayers compare?"
    assert (
        plan["output"]["canonical_question"]
        == "How do radiative cooling multilayers compare?"
    )
    assert plan["output"]["keyword_decomposition"]["keywords"] == [
        "radiative cooling multilayer inverse design optimization"
    ]
    assert plan["output"]["lenses"] == ["mechanism", "fabrication"]
    assert "optical multilayer films" in plan["output"]["inclusion_boundaries"]
    assert "optical multilayer radiative cooling" in plan["output"][
        "inclusion_boundaries"
    ]
    assert plan["output"]["scope_definition"]["main_scope"] == (
        "optical multilayer radiative cooling"
    )
    assert plan["output"]["scope_definition"]["scope_items"] == [
        "group delay engineering"
    ]
    assert "acoustic metalens" in plan["output"]["exclusion_boundaries"]
    assert plan["output"]["exclusion_boundaries"] == ["acoustic metalens"]
    assert plan["supplementary_retrieval"]["task_id"] == "task-plan"
    assert plan["supplementary_retrieval"]["gap_type"] == "claim_evidence_gap"
    assert plan["supplementary_retrieval"]["discovery_mode"] == "generated_only"
    assert plan["supplementary_retrieval"]["discovery_queries"] == [
        "radiative cooling multilayer inverse design optimization"
    ]
    assert plan["supplementary_retrieval"]["topic_scope"]["lenses"] == [
        "mechanism",
        "fabrication",
    ]
    contract = derive_topic_scope_contract(plan)
    assert contract.valid
    assert {"mechanism", "fabrication"} <= set(contract.lenses)
    assert "optical multilayer films" in contract.inclusion_boundaries
    assert "group delay engineering" in contract.inclusion_boundaries
    assert contract.allowlist_terms
    assert any(
        "optical multilayer radiative cooling" in term.casefold()
        for term in contract.allowlist_terms
    )
    assert contract.canonical_question == "How do radiative cooling multilayers compare?"
    assert contract.search_queries() == [
        "radiative cooling multilayer inverse design optimization"
    ]
    assert contract.canonical_question not in contract.search_queries()
    assert not any(
        broad in contract.search_queries()
        for broad in (
            "mechanism",
            "fabrication",
            "group delay engineering",
            "optical multilayer films",
        )
    )
    assert "acoustic metalens" in contract.exclusion_boundaries


def test_discovery_search_list_uses_only_generated_queries() -> None:
    registry = _registry()
    task = _task(task_id="task-bounded")
    resolved = registry.resolve(task.context_refs)
    records = [
        {
            "query_id": "q1",
            "text": "measured cooling power multilayer inverse design",
            "decision": "keep",
        },
        {
            "query_id": "q2",
            "text": "fabrication tolerance radiative cooling multilayer",
            "decision": "keep",
        },
    ]
    plan = build_supplementary_query_plan(task, resolved, records)
    contract = derive_topic_scope_contract(plan)
    assert contract.valid
    generated = [record["text"] for record in records]
    assert contract.search_queries() == generated
    assert contract.canonical_question not in contract.search_queries()
    assert not any(
        broad in contract.search_queries()
        for broad in (
            "mechanism",
            "fabrication",
            "group delay engineering",
            "optical multilayer films",
        )
    )
    assert "acoustic metalens" in contract.exclusion_boundaries
    assert plan["supplementary_retrieval"]["discovery_mode"] == "generated_only"
    assert plan["supplementary_retrieval"]["discovery_queries"] == generated


def test_retrieve_callback_passes_bounded_discovery_plan_to_prepare(
    tmp_path,
) -> None:
    prepare_calls: list = []
    work_root = tmp_path / "work"
    retrieve = make_literature_retrieve_callback(
        work_root=work_root,
        results_limit=3,
        snippet_limit=3,
        prepare_fn=_fake_prepare(prepare_calls),
        create_empty_kb_fn=_fake_create_empty_kb([]),
    )
    task = _task(task_id="task-bounded-retrieve")
    registry = _registry()
    context = registry.resolve(task.context_refs)
    records = [
        {
            "query_id": "q1",
            "text": "measured cooling power multilayer inverse design",
            "decision": "keep",
        },
        {
            "query_id": "q2",
            "text": "fabrication tolerance radiative cooling multilayer",
            "decision": "keep",
        },
    ]
    retrieve(task, records, context, _execution_meta(attempt_id="attempt:1"))
    assert len(prepare_calls) == 1
    plan = json.loads(
        Path(prepare_calls[0]["query_plan_path"]).read_text(encoding="utf-8")
    )
    assert plan["supplementary_retrieval"]["discovery_queries"] == [
        record["text"] for record in records
    ]
    contract = derive_topic_scope_contract(plan)
    assert contract.search_queries() == [record["text"] for record in records]
    assert contract.canonical_question not in contract.search_queries()
    assert not any(
        broad in contract.search_queries()
        for broad in (
            "mechanism",
            "fabrication",
            "group delay engineering",
            "optical multilayer films",
        )
    )
    assert contract.canonical_question == "How do radiative cooling multilayers compare?"
    assert "acoustic metalens" in contract.exclusion_boundaries


def test_retrieval_outcome_reports_admitted_candidates_from_ledger(
    tmp_path,
) -> None:
    work_root = tmp_path / "work"
    prepare_calls: list = []
    ledger_rows = [
        {
            "paper_id": "p1",
            "doi": "10.1/p1",
            "title": "Admitted one",
            "year": 2024,
            "venue": "J",
            "material_status": "s2_body",
            "admitted_to_downstream": True,
        },
        {
            "paper_id": "p2",
            "doi": "10.1/p2",
            "title": "Admitted two",
            "year": 2023,
            "venue": "J",
            "material_status": "abstract_claim",
            "admitted_to_downstream": True,
        },
        {
            "paper_id": "p3",
            "doi": "10.1/p3",
            "title": "Rejected",
            "year": 2022,
            "venue": "J",
            "material_status": "discovery_only",
            "admitted_to_downstream": False,
        },
    ]

    def prepare(**kwargs):
        prepare_calls.append(kwargs)
        work_dir = Path(kwargs["work_dir"])
        ledger_path = work_dir / "S2_MATERIAL_FLOW_LEDGER.json"
        ledger_path.write_text(
            json.dumps(
                {
                    "summary": {"paper_count": 3, "admitted_paper_count": 2},
                    "papers": ledger_rows,
                }
            ),
            encoding="utf-8",
        )
        report = {
            "status": "completed",
            "runtime_kb_sqlite": str(work_dir / "review_knowledge_base.s2.sqlite"),
            "graph_path": str(work_dir / "S2_LITERATURE_GRAPH.json"),
            "material_flow_ledger_path": str(ledger_path),
            "telemetry_path": str(work_dir / "S2_QUERY_TELEMETRY.json"),
            "external_query_runs": [{"query": "gap query", "status_code": 200}],
            "s2_query_telemetry": {"schema_version": "x", "graph_searches": 1},
            "material_flow_summary": {"admitted_paper_count": 2},
            "search_queries": ["gap query"],
            "report_sha256": "x",
        }
        (work_dir / "S2_BOOTSTRAP_REPORT.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        return report

    retrieve = make_literature_retrieve_callback(
        work_root=work_root,
        results_limit=3,
        snippet_limit=3,
        prepare_fn=prepare,
        create_empty_kb_fn=_fake_create_empty_kb([]),
    )
    task = _task(task_id="task-ledger")
    registry = _registry()
    context = registry.resolve(task.context_refs)
    records = [{"query_id": "q1", "text": "gap query", "decision": "keep"}]
    outcome = retrieve(task, records, context, _execution_meta(attempt_id="attempt:1"))
    assert len(prepare_calls) == 1
    assert outcome.to_dict()["candidate_count"] == 2
    assert [candidate["paper_id"] for candidate in outcome.candidates] == [
        "p1",
        "p2",
    ]
    assert all("text" not in candidate for candidate in outcome.candidates)
    assert all(
        set(candidate)
        <= {
            "paper_id",
            "doi",
            "title",
            "year",
            "venue",
            "material_status",
            "admitted_to_downstream",
        }
        for candidate in outcome.candidates
    )

    outcome2 = retrieve(
        task, records, context, _execution_meta(attempt_id="attempt:2")
    )
    assert outcome2.metadata["reused"] is True
    assert outcome2.to_dict()["candidate_count"] == 2
    assert outcome2.query_runs == []
    assert outcome2.metadata["external_call_count"] == 0
    assert len(prepare_calls) == 1


@pytest.mark.parametrize("mode", ["missing", "malformed"])
def test_retrieval_outcome_fails_safe_on_missing_or_malformed_ledger(
    tmp_path, mode: str
) -> None:
    work_root = tmp_path / "work"

    def prepare(**kwargs):
        work_dir = Path(kwargs["work_dir"])
        ledger_path = work_dir / "S2_MATERIAL_FLOW_LEDGER.json"
        if mode == "malformed":
            ledger_path.write_text("not json", encoding="utf-8")
        report = {
            "status": "completed",
            "runtime_kb_sqlite": str(work_dir / "review_knowledge_base.s2.sqlite"),
            "graph_path": str(work_dir / "S2_LITERATURE_GRAPH.json"),
            "material_flow_ledger_path": str(ledger_path),
            "telemetry_path": str(work_dir / "S2_QUERY_TELEMETRY.json"),
            "external_query_runs": [{"query": "gap query", "status_code": 200}],
            "s2_query_telemetry": {"schema_version": "x", "graph_searches": 1},
            "material_flow_summary": {"admitted_paper_count": 0},
            "search_queries": ["gap query"],
            "report_sha256": "x",
        }
        (work_dir / "S2_BOOTSTRAP_REPORT.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        return report

    retrieve = make_literature_retrieve_callback(
        work_root=work_root,
        results_limit=3,
        snippet_limit=3,
        prepare_fn=prepare,
        create_empty_kb_fn=_fake_create_empty_kb([]),
    )
    task = _task(task_id="task-ledger-bad")
    registry = _registry()
    context = registry.resolve(task.context_refs)
    records = [{"query_id": "q1", "text": "gap query", "decision": "keep"}]
    outcome = retrieve(task, records, context, _execution_meta(attempt_id="attempt:1"))
    assert outcome.candidates == []
    assert outcome.to_dict()["candidate_count"] == 0
    assert outcome.adequate is True
    assert [run["query"] for run in outcome.query_runs] == ["gap query"]


def test_generator_prompt_is_external_and_malformed_output_fails_closed(tmp_path) -> None:
    assert DEFAULT_GENERATOR_PROMPT.is_file()
    prompt_text = DEFAULT_GENERATOR_PROMPT.read_text(encoding="utf-8")
    assert '"F1": ["concise English keyword query one"]' in prompt_text
    assert "Never output indices" in prompt_text
    assert "reason" in prompt_text
    usage_sink: list[dict] = []
    valid_content = json.dumps(
        {
            "F1": ["measured cooling power multilayer inverse design"],
            "Q01": ["fabrication tolerance radiative cooling multilayer"],
        }
    )
    qwen_calls: list = []
    generator = make_qwen_query_generator(
        call_qwen=_fake_qwen(valid_content, qwen_calls),
        usage_sink=usage_sink.append,
    )
    result = generator(_task(), _registry().resolve(_task().context_refs))
    assert len(result.queries) == 2
    assert len(qwen_calls) == 1
    assert usage_sink and usage_sink[0]["model_tier"] == "b_plus_model"

    malformed = make_qwen_query_generator(
        call_qwen=_fake_qwen("this is not json"),
        usage_sink=usage_sink.append,
    )
    with pytest.raises(QueryGenerationError, match="not valid JSON"):
        malformed(_task(), _registry().resolve(_task().context_refs))
    empty = make_qwen_query_generator(
        call_qwen=_fake_qwen(json.dumps({"queries": []})),
        usage_sink=usage_sink.append,
    )
    with pytest.raises(QueryGenerationError, match="after correction"):
        empty(_task(), _registry().resolve(_task().context_refs))


def test_adjudicator_one_call_usage_and_malformed_conservative_keep(tmp_path) -> None:
    group = AmbiguousGroup(
        group_id="g1",
        queries=(
            QueryCandidate(
                query_id="q1",
                text="radiative cooling multilayer inverse design optimization",
                source_task_id="task-1",
            ),
        ),
        refs=(
            KnownQuery(
                query_id="h1",
                text="radiative cooling multilayer inverse design",
                source_task_id="task-seed",
                source="historical",
            ),
        ),
        reason="ambiguous",
    )
    qwen_calls: list = []
    usage_sink: list[dict] = []
    adjudicator = make_qwen_adjudicator(
        call_qwen=_fake_qwen(
            json.dumps(
                {
                    "decisions": [
                        {
                            "query_id": "q1",
                            "action": "keep",
                            "reason": "distinct optimization angle",
                        }
                    ]
                }
            ),
            qwen_calls,
        ),
        usage_sink=usage_sink.append,
    )
    decisions = adjudicator([group])
    assert len(qwen_calls) == 1
    assert decisions["decisions"][0]["query_id"] == "q1"
    assert usage_sink and usage_sink[0]["input_tokens"] == 11

    malformed_adjudicator = make_qwen_adjudicator(
        call_qwen=_fake_qwen(json.dumps({"decisions": []})),
        usage_sink=usage_sink.append,
    )
    with pytest.raises(ValueError, match="omitted decisions"):
        malformed_adjudicator([group])

    # Existing service semantics: malformed adjudicator -> conservative keep.
    service = SupplementaryRetrievalService(
        tmp_path / "svc.sqlite",
        callbacks=ServiceCallbacks(
            retrieve=lambda *a, **k: RetrievalOutcome(candidates=[], adequate=True),
            materialize=lambda *a, **k: MaterializationOutcome(
                sources=[], adequate=True, total_references=1
            ),
            adjudicator=malformed_adjudicator,
        ),
    )
    registry = _registry()
    service.submit(
        _task(
            task_id="task-seed",
            queries=("radiative cooling multilayer inverse design",),
        ),
        registry,
    )
    service.process_pending()
    submission = service.submit(
        _task(
            task_id="task-ambiguous",
            queries=("radiative cooling multilayer inverse design optimization",),
            material_requirements=("public_oa_fulltext",),
        ),
        registry,
    )
    assert submission.status == "queued"
    row = service.get_task("task-ambiguous")
    assert row["queries"][0]["needs_semantic_review"] is True
    service.close()


def test_retrieve_callback_deterministic_workdir_seed_limits_and_reuse(
    tmp_path,
) -> None:
    prepare_calls: list = []
    seed_calls: list = []
    work_root = tmp_path / "work"
    retrieve = make_literature_retrieve_callback(
        work_root=work_root,
        policy_path=tmp_path / "policy.json",
        results_limit=3,
        snippet_limit=3,
        prepare_fn=_fake_prepare(prepare_calls),
        create_empty_kb_fn=_fake_create_empty_kb(seed_calls),
    )
    task = _task(task_id="task-1")
    registry = _registry()
    context = registry.resolve(task.context_refs)
    query_records = [
        {
            "query_id": "q1",
            "text": "radiative cooling multilayer inverse design optimization",
            "decision": "keep",
        }
    ]
    outcome1 = retrieve(task, query_records, context, _execution_meta(attempt_id="attempt:1"))
    work_dir = Path(outcome1.metadata["work_dir"])
    assert work_dir.parent == work_root / "supplementary_tasks"
    assert (work_dir / "EMPTY_TASK_SEED.sqlite").is_file()
    assert (work_dir / "SUPPLEMENTARY_QUERY_PLAN.json").is_file()
    assert len(seed_calls) == 1
    assert len(prepare_calls) == 1
    prepared = prepare_calls[0]
    assert prepared["results_limit"] == 3
    assert prepared["snippet_limit"] == 3
    assert Path(prepared["base_kb_sqlite"]) == work_dir / "EMPTY_TASK_SEED.sqlite"
    assert Path(prepared["work_dir"]) == work_dir
    assert prepared["policy_path"] == tmp_path / "policy.json"
    plan = json.loads((work_dir / "SUPPLEMENTARY_QUERY_PLAN.json").read_text(encoding="utf-8"))
    assert derive_topic_scope_contract(plan).valid

    outcome2 = retrieve(task, query_records, context, _execution_meta(attempt_id="attempt:2"))
    assert outcome2.metadata["reused"] is True
    assert outcome2.metadata["external_call_count"] == 0
    assert Path(outcome2.metadata["work_dir"]) == work_dir
    assert len(prepare_calls) == 1
    assert len(seed_calls) == 1
    retrieval_report = json.loads(
        (work_dir / "SUPPLEMENTARY_RETRIEVAL_REPORT.json").read_text(encoding="utf-8")
    )
    assert retrieval_report["execution_meta"]["idempotency_key"] == "supplementary:abc"


def test_materialize_empty_ledger_skips_cards_qwen_and_embeddings(tmp_path) -> None:
    work_dir = tmp_path / "task"
    work_dir.mkdir()
    ledger_path = work_dir / "S2_MATERIAL_FLOW_LEDGER.json"
    ledger_path.write_text(
        json.dumps(
            {
                "summary": {"paper_count": 0, "admitted_paper_count": 0},
                "papers": [],
            }
        ),
        encoding="utf-8",
    )
    (work_dir / "review_knowledge_base.s2.sqlite").write_bytes(b"kb")
    (work_dir / "SUPPLEMENTARY_QUERY_PLAN.json").write_text("{}", encoding="utf-8")
    retrieval = RetrievalOutcome(
        candidates=[],
        adequate=True,
        metadata={
            "work_dir": str(work_dir),
            "material_flow_ledger_path": str(ledger_path),
            "runtime_kb_sqlite": str(work_dir / "review_knowledge_base.s2.sqlite"),
            "query_plan_path": str(work_dir / "SUPPLEMENTARY_QUERY_PLAN.json"),
        },
    )
    packet_calls: list = []
    card_calls: list = []
    unit_calls: list = []
    embed_calls: list = []
    materialize = make_literature_materialize_callback(
        build_packets_fn=_fake_build_packets(packet_calls),
        extract_cards_fn=_fake_extract_cards(card_calls),
        build_units_fn=_fake_build_units(unit_calls),
        embedder=_fake_embedder(embed_calls),
    )
    outcome = materialize(_task(), retrieval, {}, _execution_meta())
    assert outcome.adequate is False
    assert outcome.metadata["reason"] == "no_admitted_material"
    assert packet_calls == []
    assert card_calls == []
    assert unit_calls == []
    assert embed_calls == []


@pytest.mark.parametrize(
    ("material_status", "content_depth"),
    [
        ("s2_body", "fulltext"),
        ("oa_fulltext", "fulltext"),
        ("abstract_claim", "abstract_claim"),
    ],
)
def test_each_admitted_material_class_continues_to_task_local_steps(
    tmp_path, material_status: str, content_depth: str
) -> None:
    work_dir = tmp_path / "task"
    work_dir.mkdir()
    ledger_path = work_dir / "S2_MATERIAL_FLOW_LEDGER.json"
    ledger_path.write_text(
        json.dumps(
            {
                "summary": {"paper_count": 1, "admitted_paper_count": 1},
                "papers": [
                    {
                        "paper_id": "p1",
                        "title": "Paper 1",
                        "material_status": material_status,
                        "admitted_to_downstream": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (work_dir / "review_knowledge_base.s2.sqlite").write_bytes(b"kb")
    (work_dir / "SUPPLEMENTARY_QUERY_PLAN.json").write_text("{}", encoding="utf-8")
    retrieval = RetrievalOutcome(
        candidates=[],
        adequate=True,
        metadata={
            "work_dir": str(work_dir),
            "material_flow_ledger_path": str(ledger_path),
            "runtime_kb_sqlite": str(work_dir / "review_knowledge_base.s2.sqlite"),
            "query_plan_path": str(work_dir / "SUPPLEMENTARY_QUERY_PLAN.json"),
        },
    )
    packet_calls: list = []
    card_calls: list = []
    unit_calls: list = []
    embed_calls: list = []
    materialize = make_literature_materialize_callback(
        build_packets_fn=_fake_build_packets(packet_calls, material_status=material_status),
        extract_cards_fn=_fake_extract_cards(card_calls),
        build_units_fn=_fake_build_units(unit_calls, content_depth=content_depth),
        embedder=_fake_embedder(embed_calls),
    )
    outcome = materialize(_task(), retrieval, {}, _execution_meta())
    assert outcome.adequate is True
    assert len(packet_calls) == 1
    assert len(card_calls) == 1
    assert len(unit_calls) == 1
    assert len(embed_calls) == 1
    assert material_status in outcome.metadata["material_classes"]
    assert "work:abc" in outcome.metadata["identities"]
    assert (work_dir / "material_vectors" / "MATERIAL_UNITS_FINAL.json").is_file()
    assert outcome.metadata["embedding_usage"]["request_count"] >= 1
    assert outcome.metadata["qwen_usage"][0]["model_tier"] == "b_plus_model"
    for key in (
        "work_dir",
        "packets_path",
        "cards_dir",
        "units_path",
        "vector_dir",
        "final_units_path",
    ):
        assert str(outcome.metadata[key]).startswith(str(work_dir))


def test_repeated_materialization_reuses_cards_and_vectors_zero_delta(
    tmp_path,
) -> None:
    work_dir = tmp_path / "task"
    work_dir.mkdir()
    ledger_path = work_dir / "S2_MATERIAL_FLOW_LEDGER.json"
    ledger_path.write_text(
        json.dumps(
            {
                "summary": {"paper_count": 1, "admitted_paper_count": 1},
                "papers": [
                    {
                        "paper_id": "p1",
                        "material_status": "s2_body",
                        "admitted_to_downstream": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (work_dir / "review_knowledge_base.s2.sqlite").write_bytes(b"kb")
    (work_dir / "SUPPLEMENTARY_QUERY_PLAN.json").write_text("{}", encoding="utf-8")
    retrieval = RetrievalOutcome(
        candidates=[],
        adequate=True,
        metadata={
            "work_dir": str(work_dir),
            "material_flow_ledger_path": str(ledger_path),
            "runtime_kb_sqlite": str(work_dir / "review_knowledge_base.s2.sqlite"),
            "query_plan_path": str(work_dir / "SUPPLEMENTARY_QUERY_PLAN.json"),
        },
    )
    packet_calls: list = []
    card_calls: list = []
    unit_calls: list = []
    embed_calls: list = []
    materialize = make_literature_materialize_callback(
        build_packets_fn=_fake_build_packets(packet_calls),
        extract_cards_fn=_fake_extract_cards(card_calls),
        build_units_fn=_fake_build_units(unit_calls),
        embedder=_fake_embedder(embed_calls),
    )
    first = materialize(_task(), retrieval, {}, _execution_meta(attempt_id="attempt:1"))
    assert first.adequate is True
    assert len(embed_calls) == 1
    second = materialize(
        _task(), retrieval, {}, _execution_meta(attempt_id="attempt:2")
    )
    assert second.adequate is True
    assert second.metadata["reused"] is True
    assert len(packet_calls) == 1
    assert len(card_calls) == 1
    assert len(unit_calls) == 1
    assert len(embed_calls) == 1
    assert second.metadata["embedding_usage"] == {"input_tokens": 0, "request_count": 0}
    assert second.metadata["vector_result"] == {
        "requested": 0,
        "reused": 1,
        "embedded": 0,
    }


def test_visual_route_fails_closed_unless_injected(tmp_path) -> None:
    pipeline = SupplementaryRetrievalPipeline(
        tmp_path / "svc.sqlite",
        work_root=tmp_path / "work",
        results_limit=3,
        snippet_limit=3,
        prepare_fn=_fake_prepare([]),
        create_empty_kb_fn=_fake_create_empty_kb([]),
    )
    service = SupplementaryRetrievalService(
        tmp_path / "svc.sqlite",
        callbacks=pipeline.make_service_callbacks(),
    )
    registry = _registry()
    visual_task = _task("visual_material_gap", task_id="task-visual")
    service.submit(visual_task, registry)
    result = service.process_once()
    assert result.status == STATUS_FAILED
    assert "visual supplementary retrieval is not wired" in result.error
    service.close()

    visual_calls: list = []

    def injected_visual_retrieve(task, queries, context, meta):
        visual_calls.append("retrieve")
        return RetrievalOutcome(candidates=[], adequate=True, route="visual")

    def injected_visual_materialize(task, retrieval, context, meta):
        visual_calls.append("materialize")
        return MaterializationOutcome(
            sources=[{"id": "v1"}], adequate=True, total_references=1
        )

    pipeline2 = SupplementaryRetrievalPipeline(
        tmp_path / "svc2.sqlite",
        work_root=tmp_path / "work2",
        results_limit=3,
        snippet_limit=3,
        prepare_fn=_fake_prepare([]),
        create_empty_kb_fn=_fake_create_empty_kb([]),
        visual_retrieve=injected_visual_retrieve,
        visual_materialize=injected_visual_materialize,
    )
    service2 = SupplementaryRetrievalService(
        tmp_path / "svc2.sqlite",
        callbacks=pipeline2.make_service_callbacks(),
    )
    service2.submit(visual_task, registry)
    result2 = service2.process_once()
    assert result2.status == STATUS_COMMITTED
    assert visual_calls == ["retrieve", "materialize"]
    service2.close()


def test_coordinator_generate_and_submit_runs_offline_and_collects_usage(
    tmp_path,
) -> None:
    prepare_calls: list = []
    seed_calls: list = []
    packet_calls: list = []
    card_calls: list = []
    unit_calls: list = []
    embed_calls: list = []
    generator_content = json.dumps(
        {
            "F1": ["measured cooling power multilayer inverse design"],
            "Q01": ["fabrication tolerance radiative cooling multilayer"],
        }
    )
    pipeline = SupplementaryRetrievalPipeline(
        tmp_path / "svc.sqlite",
        work_root=tmp_path / "work",
        policy_path=tmp_path / "policy.json",
        results_limit=3,
        snippet_limit=3,
        qwen_call=_fake_qwen(generator_content),
        prepare_fn=_fake_prepare(prepare_calls),
        create_empty_kb_fn=_fake_create_empty_kb(seed_calls),
        build_packets_fn=_fake_build_packets(packet_calls),
        extract_cards_fn=_fake_extract_cards(card_calls),
        build_units_fn=_fake_build_units(unit_calls),
        embedder=_fake_embedder(embed_calls),
    )
    registry = _registry()
    task = _task(task_id="task-coordinator")
    submission = pipeline.generate_and_submit(task, registry)
    assert submission.status == "queued"
    results = pipeline.run_pending()
    assert len(results) == 1
    assert results[0].status == STATUS_COMMITTED
    assert pipeline.usage.query_generation
    assert pipeline.usage.material_cards
    assert pipeline.usage.embedding["request_count"] >= 1
    assert pipeline.usage.s2_telemetry
    service = SupplementaryRetrievalService(
        tmp_path / "svc.sqlite",
        callbacks=pipeline.make_service_callbacks(),
    )
    task_row = service.get_task("task-coordinator")
    assert task_row["status"] == STATUS_COMMITTED
    assert task_row["result"]["execution_meta"] is not None
    work_dir = Path(results[0].result["retrieval"]["metadata"]["work_dir"])
    assert str(work_dir).startswith(str(tmp_path / "work"))
    service.close()


def test_finalize_task_material_cache_uses_injected_embedder_and_annotations(
    tmp_path,
) -> None:
    embed_calls: list = []
    units = [
        {
            "unit_id": "unit:text:abc",
            "identity": {"chunk_id": "c1", "title": "Paper 1"},
            "durable_content": {
                "content_depth": "fulltext",
                "content_hash": "sha256:h1",
                "normalized_text": "measured result text",
            },
            "durable_content_card": {"observable_content": "result"},
        }
    ]
    cards = [
        {
            "canonical_work_id": "work:abc",
            "query_annotation": {"model_version": "qwen3.7-flash"},
            "seed_axis_assignments": [],
            "emergent_axis_candidates": [],
            "propositions": [
                {
                    "proposition_id": "p1",
                    "statement": "measured cooling power",
                    "evidence_chunk_ids": ["c1"],
                    "question_function": "support",
                }
            ],
            "background_contexts": [],
        }
    ]
    report = finalize_task_material_cache(
        units=units,
        cards=cards,
        question="How do radiative cooling multilayers compare?",
        output_dir=tmp_path / "vectors",
        embedder=_fake_embedder(embed_calls),
    )
    assert len(embed_calls) == 1
    assert report["embedding_usage"]["request_count"] == 1
    assert (tmp_path / "vectors" / "MATERIAL_UNITS_FINAL.json").is_file()
    final_units = json.loads(
        (tmp_path / "vectors" / "MATERIAL_UNITS_FINAL.json").read_text(encoding="utf-8")
    )
    assert final_units["query_annotation_summary"]["annotated_unit_count"] == 1


def _two_query_json() -> str:
    return json.dumps(
        {
            "F1": ["measured cooling power multilayer inverse design"],
            "Q01": ["fabrication tolerance radiative cooling multilayer"],
        }
    )


def _make_replay_test_pipeline(
    tmp_path,
    *,
    qwen_calls: list,
    materialize_callback=None,
    name: str = "svc.sqlite",
) -> SupplementaryRetrievalPipeline:
    qwen = _fake_qwen(_two_query_json(), qwen_calls)
    return SupplementaryRetrievalPipeline(
        tmp_path / name,
        work_root=tmp_path / "work",
        policy_path=tmp_path / "policy.json",
        results_limit=3,
        snippet_limit=3,
        generator=make_qwen_query_generator(call_qwen=qwen),
        qwen_call=qwen,
        prepare_fn=_fake_prepare([]),
        create_empty_kb_fn=_fake_create_empty_kb([]),
        build_packets_fn=_fake_build_packets([]),
        extract_cards_fn=_fake_extract_cards([]),
        build_units_fn=_fake_build_units([]),
        embedder=_fake_embedder([]),
        materialize_callback=materialize_callback,
    )


def test_committed_replay_skips_qwen_generator(tmp_path) -> None:
    qwen_calls: list = []
    pipeline = _make_replay_test_pipeline(tmp_path, qwen_calls=qwen_calls)
    registry = _registry()
    task = _task(task_id="task-replay")
    submission = pipeline.generate_and_submit(task, registry)
    assert submission.status == "queued"
    results = pipeline.run_pending()
    assert results[0].status == STATUS_COMMITTED
    calls_after_first = len(qwen_calls)
    assert calls_after_first >= 1
    semantic_usage_after_first = pipeline.semantic_relevance_usage.to_dict()
    replay = pipeline.generate_and_submit(task, registry)
    assert replay.reused is True
    assert replay.reuse_reason == "committed_replay"
    assert replay.status == STATUS_COMMITTED
    assert len(qwen_calls) == calls_after_first
    # Committed replay is zero-embedding as well as zero-Qwen.
    assert pipeline.semantic_relevance_usage.to_dict() == (
        semantic_usage_after_first
    )


def test_no_progress_replay_skips_qwen_generator(tmp_path) -> None:
    qwen_calls: list = []

    def no_progress_materialize(task, retrieval, context, meta):
        return MaterializationOutcome(
            sources=[], adequate=False, total_references=0
        )

    pipeline = _make_replay_test_pipeline(
        tmp_path,
        qwen_calls=qwen_calls,
        materialize_callback=no_progress_materialize,
    )
    registry = _registry()
    task = _task(task_id="task-no-progress-replay")
    submission = pipeline.generate_and_submit(task, registry)
    results = pipeline.run_pending()
    assert results[0].status == STATUS_NO_PROGRESS
    calls_after_first = len(qwen_calls)
    replay = pipeline.generate_and_submit(task, registry)
    assert replay.reused is True
    assert replay.reuse_reason == "no_progress_replay"
    assert len(qwen_calls) == calls_after_first


def test_failed_without_retry_skips_generator_and_retry_generates_fresh(
    tmp_path,
) -> None:
    qwen_calls: list = []
    state = {"fail": True}

    def flaky_materialize(task, retrieval, context, meta):
        if state["fail"]:
            raise RuntimeError("materialize boom")
        return MaterializationOutcome(
            sources=[], adequate=True, total_references=1
        )

    pipeline = _make_replay_test_pipeline(
        tmp_path,
        qwen_calls=qwen_calls,
        materialize_callback=flaky_materialize,
    )
    registry = _registry()
    task = _task(task_id="task-failed-replay")
    submission = pipeline.generate_and_submit(task, registry)
    results = pipeline.run_pending()
    assert results[0].status == STATUS_FAILED
    calls_after_failure = len(qwen_calls)
    assert calls_after_failure >= 1
    blocked = pipeline.generate_and_submit(task, registry)
    assert blocked.reused is False
    assert blocked.reuse_reason == "failed_requires_explicit_retry"
    assert len(qwen_calls) == calls_after_failure
    state["fail"] = False
    retried = pipeline.generate_and_submit(task, registry, allow_retry=True)
    assert retried.status == "queued"
    assert len(qwen_calls) == calls_after_failure + 1
    results = pipeline.run_pending()
    assert results[0].status == STATUS_COMMITTED


def test_already_active_replay_skips_qwen_generator(tmp_path) -> None:
    qwen_calls: list = []
    pipeline = _make_replay_test_pipeline(tmp_path, qwen_calls=qwen_calls)
    registry = _registry()
    task = _task(task_id="task-active")
    submission = pipeline.generate_and_submit(task, registry)
    assert submission.status == "queued"
    calls_after_first = len(qwen_calls)
    active = pipeline.generate_and_submit(task, registry)
    assert active.reused is True
    assert active.reuse_reason == "already_active"
    assert len(qwen_calls) == calls_after_first
    pipeline.run_pending()


def test_invalid_task_identity_fails_before_generator(tmp_path) -> None:
    qwen_calls: list = []
    pipeline = _make_replay_test_pipeline(tmp_path, qwen_calls=qwen_calls)
    registry = _registry()
    bad_task = _task(task_id="../bad")
    with pytest.raises(ValueError, match="cannot preflight.*invalid_task_id"):
        pipeline.generate_and_submit(bad_task, registry)
    assert qwen_calls == []

    missing_registry = ContextRegistry()
    missing_registry.set("topic_scope", {"topic": "x"})
    with pytest.raises(Exception, match="missing_context_field"):
        pipeline.generate_and_submit(_task(task_id="task-missing"), missing_registry)
    assert qwen_calls == []


def test_direct_only_discovery_skips_review_and_foundation_expansion() -> None:
    queries = [
        "measured cooling power multilayer inverse design",
        "fabrication tolerance radiative cooling multilayer",
    ]
    seen: list[str] = []

    class StubGateway:
        def search_papers(self, query, *, limit, open_access_pdf=False):
            seen.append(query)
            return [], S2GatewayResponse(
                ok=True, status_code=200, status_category="ok"
            )

    builder = S2DiscoveryPortfolioBuilder(gateway=StubGateway())  # type: ignore[arg-type]
    portfolio = builder.discover(
        [
            ScholarFacetRequest(
                facet_id="generated_only",
                queries=queries,
                requested_roles=["review", "foundation"],
                max_results_per_query=5,
                direct_only=True,
            )
        ]
    )
    assert seen == queries
    assert [run["query"] for run in portfolio.query_runs] == queries
    assert not any(
        "review perspective roadmap" in query or "fundamental theory origin" in query
        for query in seen
    )
    assert portfolio.wave_plan == {}


def test_default_discovery_keeps_review_and_foundation_expansion() -> None:
    queries = [
        "measured cooling power multilayer inverse design",
        "fabrication tolerance radiative cooling multilayer",
    ]
    seen: list[str] = []

    class StubGateway:
        def search_papers(self, query, *, limit, open_access_pdf=False):
            seen.append(query)
            return [], S2GatewayResponse(
                ok=True, status_code=200, status_category="ok"
            )

    builder = S2DiscoveryPortfolioBuilder(gateway=StubGateway())  # type: ignore[arg-type]
    builder.discover(
        [
            ScholarFacetRequest(
                facet_id="ordinary",
                queries=queries,
                requested_roles=["review", "foundation"],
                max_results_per_query=5,
            )
        ]
    )
    assert any("review perspective roadmap" in query for query in seen)
    assert any("fundamental theory origin" in query for query in seen)


def test_discover_keyword_direct_only_overrides_facet_flag() -> None:
    queries = ["measured cooling power multilayer inverse design"]
    seen: list[str] = []

    class StubGateway:
        def search_papers(self, query, *, limit, open_access_pdf=False):
            seen.append(query)
            return [], S2GatewayResponse(
                ok=True, status_code=200, status_category="ok"
            )

    builder = S2DiscoveryPortfolioBuilder(gateway=StubGateway())  # type: ignore[arg-type]
    builder.discover(
        [
            ScholarFacetRequest(
                facet_id="keyword-override",
                queries=queries,
                requested_roles=["review", "foundation"],
                max_results_per_query=5,
            )
        ],
        direct_only=True,
    )
    assert seen == queries


def test_query_plan_contains_task_policy_switches_for_all_gap_types() -> None:
    registry = _registry()
    records = [
        {
            "query_id": "q1",
            "text": "bounded gap query for the missing unit",
            "decision": "keep",
        }
    ]
    for gap_type in GAP_TYPES:
        task = _task(gap_type, task_id=f"policy-plan-{gap_type}")
        resolved = registry.resolve(task.context_refs)
        plan = build_supplementary_query_plan(task, resolved, records)
        marker = plan["supplementary_retrieval"]
        policy = marker["expansion_policy"]
        assert policy["gap_type"] == gap_type
        assert marker["allow_graph_expansion"] == policy["allow_graph_expansion"]
        assert marker["allow_role_expansion"] == policy["allow_role_expansion"]
        assert marker["allow_exact_paper_followup"] == (
            policy["allow_exact_paper_followup"]
        )
        assert marker["allow_batch_enrichment"] == (
            policy["allow_batch_enrichment"]
        )
        assert marker["allow_oa_fulltext_fallback"] == (
            policy["allow_oa_fulltext_fallback"]
        )
        assert marker["allow_reference_expansion"] == (
            policy["allow_reference_expansion"]
        )
        assert marker["allow_citation_expansion"] == (
            policy["allow_citation_expansion"]
        )
        assert marker["allow_recommendation_expansion"] == (
            policy["allow_recommendation_expansion"]
        )
        assert marker["allow_multi_seed_graph"] == (
            policy["allow_multi_seed_graph"]
        )
        assert marker["allow_visual_processing"] == (
            policy["allow_visual_processing"]
        )
        assert marker["graph_modes"] == policy["graph_modes"]
        assert marker["result_cap"] == policy["result_cap"]
        assert marker["extra_request_cap"] == policy["extra_request_cap"]
        for cap_key in (
            "s2_snippet_results_per_query_cap",
            "s2_precise_paper_cap",
            "batch_enrichment_paper_cap",
            "oa_fulltext_paper_cap",
            "abstract_claim_paper_cap",
            "graph_seed_cap",
        ):
            assert marker[cap_key] == policy[cap_key]
        contract = derive_topic_scope_contract(plan)
        assert contract.search_queries() == marker["discovery_queries"]
        repaired = marker["discovery_queries"][0]
        assert "bounded gap query for the missing unit" in repaired
        record = marker["query_records"][0]
        assert record["background_prefix_used"]
        assert repaired.startswith(record["background_prefix_used"])
        assert contract.canonical_question not in contract.search_queries()


def test_background_cue_repairs_naked_query_and_is_audited(tmp_path) -> None:
    registry = _registry()
    task = _task("claim_evidence_gap", task_id="task-background-cue")
    resolved = registry.resolve(task.context_refs)
    naked = {
        "query_id": "q1",
        "text": "sodium channel gating kinetics",
        "decision": "keep",
    }
    plan = build_supplementary_query_plan(task, resolved, [naked])
    marker = plan["supplementary_retrieval"]
    cue = marker["search_background_cue"]
    assert cue
    assert marker["search_background_context"] == {
        "main_scope": "optical multilayer radiative cooling",
        "lenses": ["mechanism", "fabrication"],
        "dynamic_axes": ["multilayer mechanism"],
        # claim_evidence_gap does not project section_task; the audit mirrors
        # exactly the task-specific projected context.
        "section_task_title": "",
    }
    record = marker["query_records"][0]
    assert record["background_cue_applied"] is True
    repaired = marker["discovery_queries"][0]
    assert record["background_prefix_used"]
    assert repaired.startswith(record["background_prefix_used"])
    assert "sodium channel gating kinetics" in repaired
    assert len(repaired) <= 240
    # The safeguard repairs with the compact background cue, never the full
    # user question, and never splits context words into separate searches.
    assert resolved["user_question"] not in repaired
    assert marker["discovery_queries"] == [repaired]
    # The candidate relevance layer sees the exact same compact context.
    contract = derive_topic_scope_contract(plan)
    assert contract.discovery_mode == "generated_only"
    assert contract.search_background_cue == cue
    assert contract.search_background_terms
    assert contract.search_queries() == [repaired]
    assert contract.canonical_question not in contract.search_queries()
    # The exact compact generation context is durably carried and reachable
    # from the generated_only contract/decision path.
    relevance_context = marker["relevance_context"]
    assert relevance_context["search_background_cue"] == cue
    assert relevance_context["task_id"] == task.task_id
    assert relevance_context["gap_type"] == task.gap_type
    assert relevance_context["expansion_policy"]["gap_type"] == (
        task.gap_type
    )
    assert "user_question" in relevance_context
    assert "topic_scope" in relevance_context
    assert relevance_context["context_fields"] == sorted(
        set(task.context_refs)
    )
    assert contract.relevance_context == relevance_context
    assert contract.relevance_context_sha256
    decision = _scope_match(contract, "sodium channel gating kinetics")
    assert decision["relevance_context_sha256"] == (
        contract.relevance_context_sha256
    )
    assert decision["relevance_context_field_count"] == len(relevance_context)

    section_task = _task(
        "section_argument_gap", task_id="task-background-section"
    )
    section_resolved = registry.resolve(section_task.context_refs)
    section_plan = build_supplementary_query_plan(
        section_task, section_resolved, [naked]
    )
    section_marker = section_plan["supplementary_retrieval"]
    assert section_marker["search_background_context"][
        "section_task_title"
    ] == "Mechanism"
    section_record = section_marker["query_records"][0]
    assert section_record["background_prefix_used"]
    assert section_marker["discovery_queries"][0].startswith(
        section_record["background_prefix_used"]
    )


def test_service_submit_receives_background_qualified_query(tmp_path) -> None:
    qwen_calls: list = []
    semantic_calls: list = []

    def naked_generator(task, context):
        return QueryGenerationResult(
            queries=["sodium channel gating kinetics"],
            records=[{
                "query": "sodium channel gating kinetics",
                "coverage_ids": ["missing_fact_unit_1"],
                "reason": "naked gap direction",
            }],
            usage={},
        )

    pipeline = SupplementaryRetrievalPipeline(
        tmp_path / "svc.sqlite",
        work_root=tmp_path / "work",
        generator=naked_generator,
        retrieve_callback=lambda *args, **kwargs: None,
        materialize_callback=lambda *args, **kwargs: None,
        adjudicator=lambda groups: {"decisions": []},
        semantic_embedder=_semantic_fake_embedder(semantic_calls),
    )
    registry = _registry()
    task = _task("claim_evidence_gap", task_id="task-qualified-submit")
    submission = pipeline.generate_and_submit(task, registry)
    assert submission.status == "queued"
    # One batched semantic qualification call (cue + all generated queries).
    assert pipeline.semantic_relevance_usage.request_count == 1
    assert pipeline.semantic_relevance_usage.embed_calls == 1
    assert len(semantic_calls) == 1
    service = SupplementaryRetrievalService(tmp_path / "svc.sqlite")
    try:
        row = service.get_task(task.task_id)
        stored = row["queries"]
        assert len(stored) == 1
        stored_text = stored[0]["text"]
        assert stored_text.startswith("optical ")
        assert "sodium channel gating kinetics" in stored_text
        assert registry.fields["user_question"] not in stored_text
        # Python-derived coverage metadata is persisted, not discarded.
        assert stored[0]["coverage_ids"] == ["missing_fact_unit_1"]
        assert stored[0]["generation_reasons"] == ["naked gap direction"]
        # Durable dedup saw the qualified string, not the naked gap.
        naked_key = service._idempotency_key(
            task, registry, ["sodium channel gating kinetics"]
        )
        qualified_key = service._idempotency_key(
            task, registry, [stored_text]
        )
        assert row["idempotency_key"] == qualified_key
        assert row["idempotency_key"] != naked_key
    finally:
        service.close()


def test_different_backgrounds_prevent_naked_gap_collision(tmp_path) -> None:
    import copy

    from optomind_research.runtime.supplementary_retrieval_contract import (
        CONTEXT_FIELD_CATALOG,
    )

    def generator(task, context):
        return QueryGenerationResult(
            queries=["near-field truncation error"],
            records=[{
                "query": "near-field truncation error",
                "coverage_ids": ["missing_fact_unit_1"],
                "reason": "same naked gap in two domains",
            }],
            usage={},
        )

    def registry_with_main_scope(main_scope: str) -> ContextRegistry:
        base = _registry()
        registry = ContextRegistry()
        for field_id in CONTEXT_FIELD_CATALOG:
            registry.set(field_id, copy.deepcopy(base.fields[field_id]))
        topic_scope = dict(registry.fields["topic_scope"])
        topic_scope["main_scope"] = main_scope
        registry.set("topic_scope", topic_scope)
        return registry.freeze()

    optical = registry_with_main_scope(
        "optical electromagnetic PINN simulation"
    )
    underwater = registry_with_main_scope(
        "underwater acoustic propagation modeling"
    )
    pipeline = SupplementaryRetrievalPipeline(
        tmp_path / "svc.sqlite",
        work_root=tmp_path / "work",
        generator=generator,
        retrieve_callback=lambda *args, **kwargs: None,
        materialize_callback=lambda *args, **kwargs: None,
        adjudicator=lambda groups: {"decisions": []},
        semantic_embedder=_semantic_fake_embedder([]),
    )
    first = pipeline.generate_and_submit(
        _task("claim_evidence_gap", task_id="task-gap-optical"),
        optical,
    )
    second = pipeline.generate_and_submit(
        _task("claim_evidence_gap", task_id="task-gap-underwater"),
        underwater,
    )
    assert first.status == "queued"
    assert second.status == "queued"
    assert first.idempotency_key != second.idempotency_key
    service = SupplementaryRetrievalService(tmp_path / "svc.sqlite")
    try:
        optical_row = service.get_task("task-gap-optical")
        underwater_row = service.get_task("task-gap-underwater")
        optical_text = optical_row["queries"][0]["text"]
        underwater_text = underwater_row["queries"][0]["text"]
        assert optical_text.startswith("optical ")
        assert underwater_text.startswith("underwater ")
        assert optical_text != underwater_text
        assert "near-field truncation error" in optical_text
        assert "near-field truncation error" in underwater_text
    finally:
        service.close()


def test_background_cue_does_not_prefix_queries_with_shared_background(
    tmp_path,
) -> None:
    registry = _registry()
    task = _task("claim_evidence_gap", task_id="task-background-shared")
    resolved = registry.resolve(task.context_refs)
    already_grounded = {
        "query_id": "q1",
        "text": "optical multilayer radiative cooling measurement",
        "decision": "keep",
    }
    plan = build_supplementary_query_plan(task, resolved, [already_grounded])
    marker = plan["supplementary_retrieval"]
    assert marker["query_records"][0]["background_cue_applied"] is False
    assert marker["discovery_queries"] == [
        "optical multilayer radiative cooling measurement"
    ]


def test_background_qualification_requires_two_distinctive_tokens_and_preserves_tail(
    tmp_path,
) -> None:
    registry = _registry()
    task = _task("claim_evidence_gap", task_id="task-qualification")
    resolved = registry.resolve(task.context_refs)

    def plan_for(query: str) -> dict:
        return build_supplementary_query_plan(
            task,
            resolved,
            [{"query_id": "q1", "text": query, "decision": "keep"}],
        )

    # One generic workflow token is not enough to prove background grounding.
    generic_only = plan_for("radiative research methods")
    assert generic_only["supplementary_retrieval"]["query_records"][0][
        "background_cue_applied"
    ] is True
    # A method-only label (PINN) is not enough either.
    method_only = plan_for("PINN near-field truncation")
    assert method_only["supplementary_retrieval"]["query_records"][0][
        "background_cue_applied"
    ] is True
    # Two distinctive shared cue tokens mean the query is already grounded.
    qualified = plan_for("optical multilayer inverse design")
    assert qualified["supplementary_retrieval"]["query_records"][0][
        "background_cue_applied"
    ] is False
    # Long query: the repair keeps the full precise tail and only shortens the
    # background prefix to fit the 240-char bound.
    precise_tail = "x" * 220 + " precise gap tail"
    long_plan = plan_for(precise_tail)
    repaired = long_plan["supplementary_retrieval"]["query_records"][0]
    assert repaired["background_cue_applied"] is True
    assert repaired["query"].endswith(precise_tail)
    assert len(repaired["query"]) <= 240


def test_semantic_qualification_uses_similarity_not_single_word(tmp_path) -> None:
    semantic_calls: list = []
    engine = SupplementarySemanticEngine(
        embedder=_semantic_fake_embedder(semantic_calls)
    )
    cue = "optical multilayer radiative cooling"
    records = _apply_search_background_cue(
        [
            {"query": "PINN optical electromagnetic near-field truncation error"},
            {"query": "PINN underwater acoustics"},
            {"query": "optical multilayer inverse design"},
        ],
        cue,
        semantic_engine=engine,
        semantic_threshold=0.72,
    )
    # One batched embedding call for the cue plus all generated queries.
    assert len(semantic_calls) == 1
    assert semantic_calls[0] == [
        cue,
        "PINN optical electromagnetic near-field truncation error",
        "PINN underwater acoustics",
        "optical multilayer inverse design",
    ]
    on_topic, method_only, grounded = records
    assert on_topic["qualification_mode"] == "semantic"
    assert on_topic["semantic_similarity"] < 0.72
    # The query already carries the selected domain anchor ("optical"), so it
    # is never prefixed a second time even though similarity stays low.
    assert on_topic["background_cue_applied"] is False
    assert on_topic["anchor_already_present"] is True
    # Sharing only the method label PINN is not semantic grounding.
    assert method_only["qualification_mode"] == "semantic"
    assert method_only["semantic_similarity"] < 0.72
    assert method_only["background_cue_applied"] is True
    assert method_only["anchor_already_present"] is False
    # Two meaningful domain tokens plus high semantic similarity keep the
    # query as-is.
    assert grounded["semantic_similarity"] >= 0.72
    assert grounded["background_cue_applied"] is False
    assert grounded["anchor_already_present"] is True
    assert grounded["semantic_threshold"] == 0.72
    assert grounded["fallback_error_code"] == ""


def test_semantic_engine_batches_caches_and_reports_usage(tmp_path) -> None:
    semantic_calls: list = []
    engine = SupplementarySemanticEngine(
        embedder=_semantic_fake_embedder(semantic_calls)
    )
    first = engine.embed_texts(
        ["optical multilayer cooling", "PINN underwater acoustics"]
    )
    second = engine.embed_texts(
        ["optical multilayer cooling", "EUV lithography"]
    )
    assert set(first) == {
        "optical multilayer cooling",
        "pinn underwater acoustics",
    }
    assert set(second) == {
        "optical multilayer cooling",
        "euv lithography",
    }
    # First call embedded two texts; second call embedded only the new one.
    assert semantic_calls == [
        ["optical multilayer cooling", "PINN underwater acoustics"],
        ["EUV lithography"],
    ]
    assert engine.usage.embed_calls == 2
    assert engine.usage.vector_count == 3
    assert engine.usage.request_count == 2
    assert engine.cosine(
        "optical multilayer cooling", "optical multilayer cooling"
    ) == 1.0
    assert engine.cosine(
        "optical multilayer cooling", "EUV lithography"
    ) == 0.0
    # Cache reuse: no additional embedder call.
    assert len(semantic_calls) == 2
    assert engine.usage.embed_calls == 2


def test_embedding_failure_falls_back_to_lexical_qualification(tmp_path) -> None:
    def failing_embedder(texts, **kwargs):
        raise RuntimeError("offline-no-embedding")

    engine = SupplementarySemanticEngine(embedder=failing_embedder)
    cue = "optical multilayer radiative cooling"
    naked = _apply_search_background_cue(
        [{"query": "sodium channel gating kinetics"}],
        cue,
        semantic_engine=engine,
    )[0]
    assert naked["qualification_mode"] == "lexical_fallback"
    assert "embedding_failed" in naked["fallback_error_code"]
    assert naked["background_cue_applied"] is True
    assert engine.usage.failure_count == 1
    # Lexical fallback still honors the >=2 distinctive-token rule.
    grounded = _apply_search_background_cue(
        [{"query": "optical multilayer inverse design"}],
        cue,
        semantic_engine=engine,
    )[0]
    assert grounded["qualification_mode"] == "lexical_fallback"
    assert grounded["background_cue_applied"] is False
    assert engine.usage.failure_count == 2


def test_semantic_qualification_ignores_single_shared_word_when_vectors_available(
    tmp_path,
) -> None:
    cue = "optical multilayer radiative cooling"

    def bucket_embedder(texts, *, usage_accumulator=None, **kwargs):
        close_vector = [0.8, 0.0, 0.0, 0.0, 0.0, 0.6]
        far_vector = [0.1, 0.1, 0.1, 0.1, 0.1, 0.1]
        if usage_accumulator is not None:
            usage_accumulator["input_tokens"] = (
                usage_accumulator.get("input_tokens", 0) + len(texts)
            )
            usage_accumulator["request_count"] = (
                usage_accumulator.get("request_count", 0) + 1
            )
        return [
            (
                close_vector
                if "metamaterial coherence" in str(text or "").casefold()
                or str(text or "").casefold() == cue.casefold()
                else far_vector
            )
            for text in texts
        ]

    engine = SupplementarySemanticEngine(embedder=bucket_embedder)
    close_record, far_record = _apply_search_background_cue(
        [
            {"query": "optical metamaterial coherence"},
            {"query": "stock market forecasting"},
        ],
        cue,
        semantic_engine=engine,
        semantic_threshold=0.72,
    )
    # Semantic similarity decides for queries without the anchor, and an
    # already-present anchor prevents any duplicate prefix.
    assert close_record["qualification_mode"] == "semantic"
    assert close_record["semantic_similarity"] >= 0.72
    assert close_record["background_cue_applied"] is False
    assert close_record["anchor_already_present"] is True
    assert far_record["qualification_mode"] == "semantic"
    assert far_record["semantic_similarity"] < 0.72
    assert far_record["background_cue_applied"] is True
    assert far_record["anchor_already_present"] is False


def test_generated_query_cap_is_240_and_rejects_longer(tmp_path) -> None:
    task = _task("claim_evidence_gap", task_id="task-query-cap")
    context = _registry().resolve(task.context_refs)
    valid_compact = " ".join(["electromagnetic"] * 9)

    def qwen(role, messages, **kwargs):
        return {
            "content": json.dumps({
                "F1": [valid_compact],
                "Q01": ["second bounded query terms"],
            }),
            "_llm_usage": {"input_tokens": 1, "output_tokens": 1},
        }

    generator = make_qwen_query_generator(call_qwen=qwen)
    result = generator(task, context)
    assert result.queries[0] == valid_compact
    assert set(result.records[0]) == {"query", "coverage_ids", "reason"}

    def qwen_too_long(role, messages, **kwargs):
        return {
            "content": json.dumps({
                "F1": ["x" * 241],
                "Q01": ["second bounded query terms"],
            }),
            "_llm_usage": {"input_tokens": 1, "output_tokens": 1},
        }

    with pytest.raises(QueryGenerationError, match="query_invalid_length"):
        make_qwen_query_generator(call_qwen=qwen_too_long)(task, context)


def test_generated_query_validation_rejects_prose_like_background_sentence(
    tmp_path,
) -> None:
    task = _task("claim_evidence_gap", task_id="task-prose-reject")
    context = _registry().resolve(task.context_refs)
    prose_query = (
        "PINN and differentiable electromagnetic solvers for optical research "
        "including simulation credibility and the path from simulation to "
        "experiment near-field truncation error"
    )

    def qwen(role, messages, **kwargs):
        return {
            "content": json.dumps({
                "F1": ["measured cooling power multilayer inverse design"],
                "Q01": [prose_query],
            }),
            "_llm_usage": {"input_tokens": 1, "output_tokens": 1},
        }

    with pytest.raises(QueryGenerationError, match="after correction"):
        make_qwen_query_generator(call_qwen=qwen)(task, context)


def test_generated_query_validation_accepts_compact_technical_queries(
    tmp_path,
) -> None:
    task = _task("claim_evidence_gap", task_id="task-compact-ok")
    context = _registry().resolve(task.context_refs)
    queries = [
        "optical electromagnetic PINN near-field truncation error "
        "prediction accuracy",
        "physics-informed neural network electromagnetic design",
    ]

    def qwen(role, messages, **kwargs):
        return {
            "content": json.dumps({
                "F1": [queries[0]],
                "Q01": [queries[1]],
            }),
            "_llm_usage": {"input_tokens": 1, "output_tokens": 1},
        }

    result = make_qwen_query_generator(call_qwen=qwen)(task, context)
    assert result.queries == queries


def test_named_form_accepts_six_valid_queries(tmp_path) -> None:
    task = _task("claim_evidence_gap", task_id="task-six-queries")
    context = _registry().resolve(task.context_refs)
    queries = [
        "optical electromagnetic PINN near-field truncation error accuracy",
        "physics-informed neural network electromagnetic design",
        "pinn dataset-free inverse design evidence",
        "adjoint generalization validation electromagnetic",
        "evanescent wave far-field prediction error metasurface",
        "model-free physical training sim-to-experiment gap",
    ]

    def qwen(role, messages, **kwargs):
        return {
            "content": json.dumps(
                {
                    "F1": queries[:3],
                    "Q01": queries[3:],
                }
            ),
            "_llm_usage": {"input_tokens": 1, "output_tokens": 1},
        }

    result = make_qwen_query_generator(call_qwen=qwen)(task, context)
    assert len(result.queries) == 6
    assert result.queries == queries
    assert len(result.records) == 6
    assert {record["coverage_ids"][0] for record in result.records} == {
        "F1",
        "Q01",
    }
    assert all(record["reason"] for record in result.records)


def test_named_form_deduplicates_repeated_model_queries(tmp_path) -> None:
    task = _task("claim_evidence_gap", task_id="task-duplicate-queries")
    context = _registry().resolve(task.context_refs)
    repeated = "optical electromagnetic PINN near-field truncation error"
    distinct = "differentiable solver far-field validation experiment"

    def qwen(role, messages, **kwargs):
        return {
            "content": json.dumps(
                {
                    "F1": [repeated, repeated],
                    "Q01": [distinct, repeated],
                }
            ),
            "_llm_usage": {"input_tokens": 1, "output_tokens": 1},
        }

    result = make_qwen_query_generator(call_qwen=qwen)(task, context)
    assert result.queries == [repeated, distinct]
    assert len(result.records) == 2


def test_named_form_truncates_nine_queries_to_budget(tmp_path) -> None:
    task = _task("claim_evidence_gap", task_id="task-nine-queries")
    context = _registry().resolve(task.context_refs)
    queries = [
        "optical electromagnetic PINN near-field truncation error accuracy",
        "physics-informed neural network electromagnetic design",
        "pinn dataset-free inverse design evidence",
        "adjoint generalization validation electromagnetic",
        "evanescent wave far-field prediction error metasurface",
        "model-free physical training sim-to-experiment gap",
        "near-field measurement alignment tolerance far-field error",
        "spectral bias high frequency electromagnetic credibility",
        "adjoint generalization validation electromagnetic method",
    ]

    def qwen(role, messages, **kwargs):
        return {
            "content": json.dumps(
                {
                    "F1": queries[:5],
                    "Q01": queries[5:],
                }
            ),
            "_llm_usage": {"input_tokens": 1, "output_tokens": 1},
        }

    result = make_qwen_query_generator(call_qwen=qwen)(task, context)
    assert len(result.queries) == 8
    assert result.queries == queries[:8]
    normalization = result.usage["query_form_normalization"][0]
    assert normalization["overflow_count"] == 1
    assert normalization["unique_query_count"] == 8


def test_compact_structure_violation_triggers_one_correction_retry(
    tmp_path,
) -> None:
    task = _task("claim_evidence_gap", task_id="task-structure-retry")
    context = _registry().resolve(task.context_refs)
    valid_query = "electromagnetic near-field truncation error prediction accuracy"
    corrected_query = (
        "PINN differentiable near-field truncation error prediction"
    )
    method_heavy = (
        "PINN differentiable electromagnetic solvers simulation credibility "
        "path experiment optical near-field truncation error far-field "
        "prediction accuracy"
    )
    calls: list[dict] = []
    usages: list[dict] = []

    def qwen(role, messages, **kwargs):
        calls.append(
            {
                "role": role,
                "messages": messages,
                "model_tier": kwargs.get("model_tier"),
            }
        )
        if len(calls) == 1:
            content = json.dumps({
                "F1": [valid_query],
                "Q01": [method_heavy],
            })
        else:
            content = json.dumps({
                "F1": [valid_query],
                "Q01": [corrected_query],
            })
        return {
            "content": content,
            "_llm_usage": {
                "input_tokens": len(calls) * 10,
                "output_tokens": 5,
            },
        }

    generator = make_qwen_query_generator(
        call_qwen=qwen,
        usage_sink=lambda usage: usages.append(dict(usage)),
    )
    result = generator(task, context)
    assert len(calls) == 2
    assert result.queries == [valid_query, corrected_query]
    assert len(usages) == 2
    assert calls[1]["model_tier"] == "cheap_model"
    correction = calls[1]["messages"][-1]["content"]
    assert len(calls[1]["messages"]) == 2
    assert "correct_named_query_form" in correction
    assert "invalid_queries" in correction
    assert "allowed_coverage_fields" in correction
    assert "validation_errors" in correction
    assert "background_cue" in correction
    assert "method_anchors" in correction or "terms_above" in correction
    assert '"user_question"' not in correction
    # The correction retry never resends the full task context.
    assert "user_question" not in correction
    # Both attempts are auditable on the result usage.
    assert result.usage["attempt_count"] == 2
    assert len(result.usage["attempts"]) == 2
    assert result.usage["input_tokens"] == 20


def test_method_anchor_overflow_after_correction_is_soft_warning(
    tmp_path,
) -> None:
    task = _task(
        "claim_evidence_gap",
        task_id="task-method-anchor-soft",
    )
    context = _registry().resolve(task.context_refs)
    too_few_terms = "fdtd fem pinn"
    corrected_three_anchors = (
        "fdtd fem pinn near-field truncation error accuracy"
    )
    valid_second = (
        "radiative cooling multilayer far-field measurement uncertainty"
    )
    calls: list = []

    def qwen(role, messages, **kwargs):
        calls.append({"role": role, "messages": messages})
        content = json.dumps({
            "F1": [too_few_terms] if len(calls) == 1 else [
                corrected_three_anchors
            ],
            "Q01": [valid_second],
        })
        return {
            "content": content,
            "_llm_usage": {
                "input_tokens": len(calls) * 10,
                "output_tokens": 5,
            },
        }

    generator = make_qwen_query_generator(call_qwen=qwen)
    result = generator(task, context)

    # One correction pass fixed the hard below-minimum-term failure; the
    # corrected query still has 3 method anchors but is accepted.
    assert len(calls) == 2
    assert result.queries == [corrected_three_anchors, valid_second]
    assert result.usage["attempt_count"] == 2
    assert "query_has_3_method_anchors_above_limit_2" in (
        result.usage["soft_structure_warnings"]
    )
    assert result.usage["query_soft_structure_warnings"][
        corrected_three_anchors
    ] == ["query_has_3_method_anchors_above_limit_2"]


def test_correction_schema_uses_real_catalog_ids_not_literal_f1_f2(
    tmp_path,
) -> None:
    task = _task("review_structure_gap", task_id="task-schema-real-ids")
    context = _registry().resolve(task.context_refs)
    prose_query = (
        "PINN and differentiable electromagnetic solvers for optical research "
        "including simulation credibility and the path from simulation to "
        "experiment near-field truncation error"
    )
    calls: list = []

    def qwen(role, messages, **kwargs):
        calls.append({"role": role, "messages": messages})
        return {
            "content": json.dumps({
                "S01": [prose_query],
                "P1": ["second bounded query terms"],
            }),
            "_llm_usage": {},
        }

    with pytest.raises(QueryGenerationError, match="after correction"):
        make_qwen_query_generator(call_qwen=qwen)(task, context)
    assert len(calls) == 2
    correction = json.loads(calls[1]["messages"][-1]["content"])
    assert set(correction["schema"]) == {"S01", "P1"}
    assert "F1" not in correction["schema"]
    assert "F2" not in correction["schema"]
    allowed_ids = {
        entry["coverage_id"]
        for entry in correction["allowed_coverage_fields"]
    }
    assert set(correction["schema"]) <= allowed_ids


def test_structure_retry_not_triggered_for_malformed_or_nonstructural_errors(
    tmp_path,
) -> None:
    task = _task("claim_evidence_gap", task_id="task-no-retry")
    context = _registry().resolve(task.context_refs)

    calls: list = []

    def malformed(role, messages, **kwargs):
        calls.append(role)
        return {"content": "not json", "_llm_usage": {}}

    with pytest.raises(QueryGenerationError, match="not valid JSON"):
        make_qwen_query_generator(call_qwen=malformed)(task, context)
    assert len(calls) == 1


def test_named_form_rejects_legacy_anonymous_list_and_accepts_named_fields(
    tmp_path,
) -> None:
    task = _task("claim_evidence_gap", task_id="task-string-only")
    context = _registry().resolve(task.context_refs)
    calls: list = []

    def qwen_legacy_list(role, messages, **kwargs):
        calls.append(role)
        return {
            "content": json.dumps({
                "queries": [
                    "electromagnetic near-field truncation error",
                    "PINN differentiable near-field prediction",
                ]
            }),
            "_llm_usage": {},
        }

    # Legacy anonymous list is a named-form validation error: one compact
    # correction retry is issued; the unchanged response fails closed.
    with pytest.raises(QueryGenerationError, match="after correction"):
        make_qwen_query_generator(call_qwen=qwen_legacy_list)(task, context)
    assert len(calls) == 2

    def qwen_named(role, messages, **kwargs):
        return {
            "content": json.dumps({
                "F1": [
                    "electromagnetic near-field truncation error",
                    "PINN differentiable near-field prediction",
                ]
            }),
            "_llm_usage": {},
        }

    result = make_qwen_query_generator(call_qwen=qwen_named)(task, context)
    assert result.queries == [
        "electromagnetic near-field truncation error",
        "PINN differentiable near-field prediction",
    ]
    assert result.assignment[0]["mode"] == "explicit_field"
    assert result.records[0]["coverage_ids"] == ["F1"]
    assert result.records[0]["reason"].startswith("coverage: ")


def test_named_form_explicit_binding_beats_semantic_preference(
    tmp_path,
) -> None:
    catalog = [
        {
            "coverage_id": "F1",
            "description": (
                "missing fact unit: near-field truncation error"
            ),
            "target_type": "missing_fact",
            "priority": 90,
        },
        {
            "coverage_id": "F2",
            "description": (
                "missing fact unit: alignment and positioning tolerance"
            ),
            "target_type": "missing_fact",
            "priority": 90,
        },
        {
            "coverage_id": "A1",
            "description": (
                "dynamic axis: PINN differentiable solver comparison"
            ),
            "target_type": "axis",
            "priority": 70,
        },
    ]
    background_cue = "optical electromagnetic PINN near-field fidelity"
    query = "alignment positioning tolerance"
    semantic_calls: list = []
    engine = SupplementarySemanticEngine(
        embedder=_semantic_fake_embedder(semantic_calls)
    )
    records, assignments = _records_from_named_cells(
        [("F1", query)],
        background_cue=background_cue,
        catalog=catalog,
        semantic_engine=engine,
    )
    assert records[0]["coverage_ids"] == ["F1"]
    assert records[0]["reason"].startswith("coverage: ")
    assert assignments[0]["mode"] == "explicit_field"
    assert assignments[0]["coverage_id"] == "F1"
    # Audit metadata proves the text is semantically closer to F2, yet the
    # explicit F1 field assignment is never overridden.
    f1_similarity = engine.cosine(query, catalog[0]["description"])
    f2_similarity = engine.cosine(query, catalog[1]["description"])
    assert f2_similarity > f1_similarity
    assert assignments[0]["similarity"] == round(f1_similarity, 6)


def test_named_form_invalid_fields_trigger_one_correction_retry(
    tmp_path,
) -> None:
    task = _task("claim_evidence_gap", task_id="task-named-invalid")
    context = _registry().resolve(task.context_refs)
    invalid_payloads = [
        {"UNKNOWN": ["electromagnetic near-field truncation error"]},
        {"F1": "electromagnetic near-field truncation error"},
        {
            "F1": [
                "near-field truncation error",
                "near-field truncation error",
            ]
        },
    ]
    for payload in invalid_payloads:
        calls: list = []

        def qwen(role, messages, **kwargs):
            calls.append({"role": role, "messages": messages})
            return {
                "content": json.dumps(payload),
                "_llm_usage": {},
            }

        with pytest.raises(QueryGenerationError, match="after correction"):
            make_qwen_query_generator(call_qwen=qwen)(task, context)
        assert len(calls) == 2
        correction = calls[1]["messages"][-1]["content"]
        assert "allowed_coverage_fields" in correction
        assert "validation_errors" in correction


def test_local_semantic_coverage_assignment_maps_gap_queries_to_fact_targets(
    tmp_path,
) -> None:
    catalog = [
        {
            "coverage_id": "F1",
            "description": (
                "missing fact unit: near-field truncation error"
            ),
            "target_type": "missing_fact",
            "priority": 90,
        },
        {
            "coverage_id": "F2",
            "description": (
                "missing fact unit: alignment and positioning tolerance"
            ),
            "target_type": "missing_fact",
            "priority": 90,
        },
        {
            "coverage_id": "A1",
            "description": (
                "dynamic axis: PINN differentiable solver comparison"
            ),
            "target_type": "axis",
            "priority": 70,
        },
    ]
    background_cue = "optical electromagnetic PINN near-field fidelity"
    queries = [
        "near-field evanescent truncation error",
        "alignment positioning tolerance",
    ]
    semantic_calls: list = []
    engine = SupplementarySemanticEngine(
        embedder=_semantic_fake_embedder(semantic_calls)
    )
    records, assignments = _records_from_query_strings(
        queries,
        background_cue=background_cue,
        catalog=catalog,
        gap_type="claim_evidence_gap",
        semantic_engine=engine,
    )
    assert [item["mode"] for item in assignments] == [
        "semantic",
        "semantic",
    ]
    assert records[0]["coverage_ids"] == ["F1"]
    assert records[1]["coverage_ids"] == ["F2"]
    assert assignments[1]["coverage_id"] == "F2"
    # One batched cached embed call covers cue + queries + eligible targets.
    assert len(semantic_calls) == 1

    # Lexical fallback routes to the same targets without embeddings.
    lexical_records, lexical_assignments = _records_from_query_strings(
        queries,
        background_cue=background_cue,
        catalog=catalog,
        gap_type="claim_evidence_gap",
        semantic_engine=None,
    )
    assert [item["mode"] for item in lexical_assignments] == [
        "lexical_fallback",
        "lexical_fallback",
    ]
    assert lexical_records[0]["coverage_ids"] == ["F1"]
    assert lexical_records[1]["coverage_ids"] == ["F2"]


def test_background_repair_prefixes_compact_keywords_not_prose_cue(
    tmp_path,
) -> None:
    cue = (
        "PINN and differentiable electromagnetic solvers for optical research"
    )
    repaired = _apply_search_background_cue(
        [{"query": "sodium channel gating kinetics"}],
        cue,
    )[0]
    # At most one non-method, non-generic domain anchor is injected into S2;
    # the method-heavy full cue is never copied.
    compact_prefix = "electromagnetic"
    assert repaired["background_cue_applied"] is True
    assert repaired["background_prefix_used"] == compact_prefix
    assert repaired["query"].startswith(compact_prefix)
    assert repaired["query"].endswith("sodium channel gating kinetics")
    assert "PINN" not in repaired["query"]
    assert "differentiable" not in repaired["query"]
    assert "solvers" not in repaired["query"]
    assert "PINN and differentiable" not in repaired["query"]
    assert "optical research" not in repaired["query"]


def test_background_anchor_repair_is_idempotent(tmp_path) -> None:
    cue = (
        "PINN and differentiable electromagnetic solvers for optical research"
    )
    first = _apply_search_background_cue(
        [{"query": "sodium channel gating kinetics"}],
        cue,
    )[0]
    assert first["background_cue_applied"] is True
    assert first["background_prefix_used"] == "electromagnetic"
    assert first["query"] == "electromagnetic sodium channel gating kinetics"

    # Re-entry (for example pre-submit and again at plan build) must not
    # duplicate the anchor, even though the lexical/semantic threshold would
    # still consider the bare query unqualified.
    second = _apply_search_background_cue(
        [{"query": first["query"]}],
        cue,
    )[0]
    assert second["query"] == first["query"]
    assert second["background_cue_applied"] is False
    assert second["anchor_already_present"] is True
    assert second["background_prefix_used"] == "electromagnetic"
    assert second["qualification_mode"] == "lexical_fallback"


def test_semantic_anchor_repair_is_idempotent_when_similarity_stays_low(
    tmp_path,
) -> None:
    cue = (
        "PINN and differentiable electromagnetic solvers for optical research"
    )
    far_vector = [0.1, 0.1, 0.1, 0.1, 0.1, 0.1]

    def low_embedder(texts, *, usage_accumulator=None, **kwargs):
        if usage_accumulator is not None:
            usage_accumulator["input_tokens"] = (
                usage_accumulator.get("input_tokens", 0) + len(texts)
            )
            usage_accumulator["request_count"] = (
                usage_accumulator.get("request_count", 0) + 1
            )
        return [
            (
                [0.8, 0.6]
                if str(text or "").casefold() == cue.casefold()
                else list(far_vector)
            )
            for text in texts
        ]

    engine = SupplementarySemanticEngine(embedder=low_embedder)
    first = _apply_search_background_cue(
        [{"query": "sodium channel gating kinetics"}],
        cue,
        semantic_engine=engine,
        semantic_threshold=0.72,
    )[0]
    assert first["qualification_mode"] == "semantic"
    assert first["background_cue_applied"] is True
    assert first["query"].startswith("electromagnetic ")

    second = _apply_search_background_cue(
        [{"query": first["query"]}],
        cue,
        semantic_engine=engine,
        semantic_threshold=0.72,
    )[0]
    assert second["query"] == first["query"]
    assert second["background_cue_applied"] is False
    assert second["anchor_already_present"] is True
    assert second["qualification_mode"] == "semantic"
    assert second["semantic_similarity"] == first["semantic_similarity"]
    assert second["semantic_threshold"] == 0.72


def test_query_already_containing_anchor_is_never_prefixed(tmp_path) -> None:
    cue = (
        "PINN and differentiable electromagnetic solvers for optical research"
    )
    record = _apply_search_background_cue(
        [{"query": "electromagnetic near-field truncation error"}],
        cue,
    )[0]
    assert record["query"] == "electromagnetic near-field truncation error"
    assert record["background_cue_applied"] is False
    assert record["anchor_already_present"] is True
    assert record["background_prefix_used"] == "electromagnetic"


def test_default_result_cap_is_sane_and_separate_from_extra_requests(
    tmp_path,
) -> None:
    prepare_calls: list = []
    work_root = tmp_path / "work"
    retrieve = make_literature_retrieve_callback(
        work_root=work_root,
        snippet_limit=3,
        prepare_fn=_fake_prepare(prepare_calls),
        create_empty_kb_fn=_fake_create_empty_kb([]),
    )
    task = _task("claim_evidence_gap", task_id="task-default-caps")
    registry = _registry()
    context = registry.resolve(task.context_refs)
    records = [{"query_id": "q1", "text": "bounded query", "decision": "keep"}]
    retrieve(task, records, context, _execution_meta(attempt_id="attempt:1"))
    prepared = prepare_calls[0]
    # The task-policy default result cap is higher than the old diagnostic 2,
    # and is separate from the extra-request cap.
    assert prepared["results_limit"] == 16
    plan = json.loads(
        Path(prepared["query_plan_path"]).read_text(encoding="utf-8")
    )
    marker = plan["supplementary_retrieval"]
    assert marker["result_cap"] == 16
    assert marker["extra_request_cap"] == 8


def test_explicit_caller_caps_override_policy_without_touching_defaults(
    tmp_path,
) -> None:
    prepare_calls: list = []
    work_root = tmp_path / "work"
    retrieve = make_literature_retrieve_callback(
        work_root=work_root,
        results_limit=5,
        extra_request_cap=3,
        snippet_limit=3,
        prepare_fn=_fake_prepare(prepare_calls),
        create_empty_kb_fn=_fake_create_empty_kb([]),
    )
    task = _task("claim_evidence_gap", task_id="task-override-caps")
    registry = _registry()
    context = registry.resolve(task.context_refs)
    records = [{"query_id": "q1", "text": "bounded query", "decision": "keep"}]
    retrieve(task, records, context, _execution_meta(attempt_id="attempt:1"))
    prepared = prepare_calls[0]
    assert prepared["results_limit"] == 5
    plan = json.loads(
        Path(prepared["query_plan_path"]).read_text(encoding="utf-8")
    )
    marker = plan["supplementary_retrieval"]
    assert marker["result_cap"] == 5
    assert marker["extra_request_cap"] == 3
    # The policy defaults remain inspectable and untouched.
    assert marker["expansion_policy"]["result_cap"] == 16
    assert marker["expansion_policy"]["extra_request_cap"] == 8


def test_zero_snippet_cap_is_preserved_through_query_plan_callback(
    tmp_path,
) -> None:
    prepare_calls: list = []
    work_root = tmp_path / "work"
    retrieve = make_literature_retrieve_callback(
        work_root=work_root,
        snippet_limit=None,
        prepare_fn=_fake_prepare(prepare_calls),
        create_empty_kb_fn=_fake_create_empty_kb([]),
    )
    task = _task(
        "claim_evidence_gap",
        task_id="task-zero-snippet",
        metadata={"expansion_policy": {"s2_snippet_results_per_query_cap": 0}},
    )
    registry = _registry()
    context = registry.resolve(task.context_refs)
    records = [{"query_id": "q1", "text": "bounded query", "decision": "keep"}]
    retrieve(task, records, context, _execution_meta(attempt_id="attempt:1"))
    prepared = prepare_calls[0]
    # Zero must not be coerced to 1 in the supplementary callback path.
    assert prepared["snippet_limit"] == 0
    plan = json.loads(
        Path(prepared["query_plan_path"]).read_text(encoding="utf-8")
    )
    marker = plan["supplementary_retrieval"]
    assert marker["s2_snippet_results_per_query_cap"] == 0
    assert marker["expansion_policy"]["s2_snippet_results_per_query_cap"] == 0
    # The precise route cap stays independent.
    assert marker["s2_precise_paper_cap"] == 8


def test_generate_and_submit_passes_projected_context_not_full_registry(
    tmp_path,
) -> None:
    captured: dict = {}

    def generator(task, context):
        captured["task"] = task
        captured["context"] = context
        return QueryGenerationResult(
            queries=["bounded gap query"],
            records=[{
                "query": "bounded gap query",
                "coverage_ids": ["cooling_power_measured"],
                "reason": "missing measured value",
            }],
            usage={},
        )

    pipeline = SupplementaryRetrievalPipeline(
        tmp_path / "svc.sqlite",
        work_root=tmp_path / "work",
        generator=generator,
        retrieve_callback=lambda *args, **kwargs: None,
        materialize_callback=lambda *args, **kwargs: None,
        adjudicator=lambda groups: {"decisions": []},
        semantic_embedder=_semantic_fake_embedder([]),
    )
    registry = _registry()
    task = _task("claim_evidence_gap", task_id="task-projected")
    submission = pipeline.generate_and_submit(task, registry)
    assert submission.status == "queued"
    context = captured["context"]
    expected_ids = set(task.context_refs)
    assert set(context) - {"task_metadata"} == expected_ids
    assert "historical_queries" not in context
    assert "concurrent_queries" not in context
    metadata = context["task_metadata"]
    assert metadata["gap_type"] == "claim_evidence_gap"
    assert metadata["expansion_policy"]["gap_type"] == "claim_evidence_gap"
    assert metadata["expansion_policy"]["result_cap"] == 16
    assert metadata["expansion_policy"]["extra_request_cap"] == 8


def test_committed_replay_with_policy_metadata_skips_generator(
    tmp_path,
) -> None:
    qwen_calls: list = []
    pipeline = _make_replay_test_pipeline(tmp_path, qwen_calls=qwen_calls)
    registry = _registry()
    task = _task(
        task_id="task-policy-replay",
        metadata={"expansion_policy": {"result_cap": 12}},
    )
    submission = pipeline.generate_and_submit(task, registry)
    assert submission.status == "queued"
    results = pipeline.run_pending()
    assert results[0].status == STATUS_COMMITTED
    calls_after_first = len(qwen_calls)
    replay = pipeline.generate_and_submit(task, registry)
    assert replay.reused is True
    assert replay.reuse_reason == "committed_replay"
    assert len(qwen_calls) == calls_after_first


def test_generator_payload_includes_all_projected_cells_and_context_fields(
    tmp_path,
) -> None:
    captured: dict = {}

    def qwen(role, messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        captured["payload"] = payload
        if payload.get("task_id") == "task-context":
            named_queries = {
                "S01": ["bounded gap query one"],
                "RF1": ["bounded gap query two"],
            }
        else:
            named_queries = {
                "F1": ["measured cooling power multilayer inverse design"],
                "Q01": ["fabrication tolerance radiative cooling multilayer"],
            }
        return {
            "content": json.dumps(named_queries),
            "_llm_usage": {"input_tokens": 1, "output_tokens": 1},
        }

    generator = make_qwen_query_generator(call_qwen=qwen)
    registry = _registry()
    task = _task("review_structure_gap", task_id="task-context")
    projected = project_context_for_task(task, registry)
    result = generator(task, projected)
    assert result.queries == ["bounded gap query one", "bounded gap query two"]
    payload = captured["payload"]
    required = set(GAP_TYPE_REQUIRED_CONTEXT_FIELDS["review_structure_gap"])
    for field_id in required:
        assert field_id in payload
    assert payload["context_fields"] == sorted(required)
    assert "task_metadata" not in payload
    assert payload["reviewer_feedback"] == {
        "mentor": "needs direct measurement support"
    }
    assert payload["author_revision_history"] == [
        {"revision": 1, "outcome": "still_open"}
    ]
    assert payload["current_review_structure"]["existing_sections"] == [
        {"section_id": "S01"}
    ]
    assert payload["paper_introduction_conclusion_excerpts"][
        "current_paper_introduction_excerpt"
    ]
    assert payload["expansion_policy"]["gap_type"] == "review_structure_gap"
    assert payload["exclusion_boundaries"] == ["acoustic metalens"]
    coverage_catalog = payload["coverage_catalog"]
    assert coverage_catalog
    assert all(
        entry.get("coverage_id")
        and entry.get("description")
        and entry.get("target_type")
        and entry.get("priority") is not None
        for entry in coverage_catalog
    )
    # Fields outside this gap type's subset must not leak into the payload.
    assert "missing_fact_units" not in payload
    assert "visual_slots" not in payload
    # Generated queries target missing fact units, never the full question,
    # and the protocol output stays exactly {queries:[{query,coverage_ids,reason}]}.
    assert all(
        query != registry.resolve(task.context_refs)["user_question"]
        for query in result.queries
    )
    # Python derives the full internal records from the compact cells.
    assert set(result.records[0]) == {"query", "coverage_ids", "reason"}
    assert result.records[0]["coverage_ids"] == ["S01"]
    assert result.records[1]["coverage_ids"] == ["RF1"]
    primary_id = "S01"
    primary_entry = next(
        entry
        for entry in coverage_catalog
        if entry["coverage_id"] == primary_id
    )
    assert result.records[0]["reason"].startswith("coverage: ")
    assert primary_entry["description"] in result.records[0]["reason"]

    claim_task = _task("claim_evidence_gap", task_id="task-context-claim")
    claim_projected = project_context_for_task(claim_task, registry)
    claim_result = generator(claim_task, claim_projected)
    claim_payload = captured["payload"]
    assert "missing_fact_units" in claim_payload
    assert claim_payload["missing_fact_units"] == ["cooling_power_measured"]
    assert all(
        query != claim_payload["user_question"]
        for query in claim_result.queries
    )


def test_named_form_generator_reads_projected_context_for_all_gap_types(
    tmp_path,
) -> None:
    captured: dict = {}

    def qwen(role, messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        captured["payload"] = payload
        catalog_ids = [
            str(entry.get("coverage_id") or "")
            for entry in payload.get("coverage_catalog") or []
            if str(entry.get("coverage_id") or "")
        ]
        first = catalog_ids[0]
        second = catalog_ids[1] if len(catalog_ids) > 1 else catalog_ids[0]
        return {
            "content": json.dumps({
                first: ["bounded gap query one"],
                second: ["bounded gap query two"],
            }),
            "_llm_usage": {"input_tokens": 1, "output_tokens": 1},
        }

    generator = make_qwen_query_generator(call_qwen=qwen)
    registry = _registry()
    for gap_type in GAP_TYPES:
        task = _task(gap_type, task_id=f"task-context-{gap_type}")
        projected = project_context_for_task(task, registry)
        result = generator(task, projected)
        payload = captured["payload"]
        required = set(GAP_TYPE_REQUIRED_CONTEXT_FIELDS[gap_type])
        # Exactly the task-specific projected cells are forwarded; a field
        # from another gap type cannot leak in.
        assert payload["context_fields"] == sorted(required)
        assert payload["gap_type"] == gap_type
        assert payload["expansion_policy"]["gap_type"] == gap_type
        catalog_ids = {
            str(entry.get("coverage_id") or "")
            for entry in payload["coverage_catalog"]
        }
        assert len(result.records) == 2
        for record in result.records:
            assert set(record) == {"query", "coverage_ids", "reason"}
            assert len(record["coverage_ids"]) == 1
            assert record["coverage_ids"][0] in catalog_ids
            assert record["reason"].startswith("coverage: ")
        assert all(
            result.assignment[index]["mode"] == "explicit_field"
            for index in range(2)
        )


def _filter_materialize_fixture(
    tmp_path,
    *,
    units: list[dict],
    card_relevance: dict[str, str],
):
    work_dir = tmp_path / "task"
    work_dir.mkdir()
    ledger_path = work_dir / "S2_MATERIAL_FLOW_LEDGER.json"
    work_ids = sorted({str(unit.get("work_id") or "") for unit in units})
    ledger_path.write_text(
        json.dumps(
            {
                "summary": {"paper_count": len(work_ids), "admitted_paper_count": len(work_ids)},
                "papers": [
                    {
                        "paper_id": f"p{index}",
                        "title": f"Paper {work_id}",
                        "material_status": "s2_body",
                        "admitted_to_downstream": True,
                    }
                    for index, work_id in enumerate(work_ids)
                ],
            }
        ),
        encoding="utf-8",
    )
    (work_dir / "review_knowledge_base.s2.sqlite").write_bytes(b"kb")
    (work_dir / "SUPPLEMENTARY_QUERY_PLAN.json").write_text("{}", encoding="utf-8")
    retrieval = RetrievalOutcome(
        candidates=[],
        adequate=True,
        metadata={
            "work_dir": str(work_dir),
            "material_flow_ledger_path": str(ledger_path),
            "runtime_kb_sqlite": str(work_dir / "review_knowledge_base.s2.sqlite"),
            "query_plan_path": str(work_dir / "SUPPLEMENTARY_QUERY_PLAN.json"),
        },
    )

    def build_packets(**kwargs):
        packets = [
            {
                "canonical_work_id": work_id,
                "material_classes": ["s2_body"],
                "question": "question",
            }
            for work_id in work_ids
        ]
        return {
            "question": "question",
            "packets": packets,
            "source_material_record_count": len(packets),
        }

    def extract_cards(**kwargs):
        cards_dir = Path(kwargs["output_dir"]) / "cards"
        cards_dir.mkdir(parents=True, exist_ok=True)
        for work_id, relevance in card_relevance.items():
            (cards_dir / f"{work_id.replace(':', '_')}.json").write_text(
                json.dumps(
                    {
                        "card": {
                            "canonical_work_id": work_id,
                            "question_relevance": relevance,
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        return {
            "rows": [
                {
                    "status": "passed",
                    "llm_usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "model_tier": "b_plus_model",
                    },
                }
            ],
            "selected_work_count": len(card_relevance),
            "new_attempt_count": 1,
            "reused_count": 0,
            "successful_card_count": len(card_relevance),
            "failed_count": 0,
        }

    def build_units(**kwargs):
        return {"units": [dict(unit) for unit in units]}

    embed_calls: list = []
    materialize = make_literature_materialize_callback(
        build_packets_fn=build_packets,
        extract_cards_fn=extract_cards,
        build_units_fn=build_units,
        embedder=_fake_embedder(embed_calls),
    )
    task = _task()
    outcome = materialize(task, retrieval, {}, _execution_meta())
    return outcome, work_dir, embed_calls, materialize, retrieval, task


def _filter_unit(unit_id: str, work_id: str, title: str) -> dict:
    return {
        "unit_id": unit_id,
        "work_id": work_id,
        "identity": {"chunk_id": unit_id, "title": title},
        "durable_content": {
            "content_depth": "fulltext",
            "content_hash": f"sha256:{unit_id}",
            "normalized_text": f"text {title}",
        },
        "durable_content_card": {"observable_content": f"card {title}"},
        "query_annotations": [],
    }


def test_materialize_filters_out_of_scope_before_embedding(tmp_path) -> None:
    units = [
        _filter_unit("u1", "work:a", "Central Paper a"),
        _filter_unit("u2", "work:a", "Central Paper a"),
        _filter_unit("u3", "work:b", "Excluded Paper b"),
        _filter_unit("u4", "work:c", "Contextual Paper c"),
        _filter_unit("u5", "work:d", "Uncarded Paper d"),
    ]
    outcome, work_dir, embed_calls, _materialize, _retrieval, _task = (
        _filter_materialize_fixture(
            tmp_path,
            units=units,
            card_relevance={
                "work:a": "central",
                "work:b": "out_of_scope",
                "work:c": "contextual",
            },
        )
    )
    audit = outcome.metadata["relevance_filter_audit"]
    assert audit["counts"]["pre_filter_card_work_count"] == 3
    assert audit["counts"]["post_filter_card_work_count"] == 2
    assert audit["counts"]["pre_filter_unit_work_count"] == 4
    assert audit["counts"]["post_filter_unit_work_count"] == 3
    assert audit["counts"]["pre_filter_unit_count"] == 5
    assert audit["counts"]["post_filter_unit_count"] == 4
    assert audit["excluded_work_ids"] == ["work:b"]
    assert audit["excluded_works"] == [
        {
            "work_id": "work:b",
            "question_relevance": "out_of_scope",
            "reason": "question_relevance_out_of_scope",
        }
    ]
    assert audit["unclassified_work_ids"] == ["work:d"]
    assert audit["all_out_of_scope"] is False
    assert outcome.adequate is True
    assert outcome.total_references == 4
    assert outcome.metadata["identities"] == [
        "work:a",
        "work:c",
        "work:d",
    ]
    assert outcome.metadata["material_classes"] == ["s2_body"]
    assert len(embed_calls) == 1
    embedded_text = " ".join(embed_calls[0])
    assert "Excluded Paper b" not in embedded_text
    assert "Central Paper a" in embedded_text
    assert "Contextual Paper c" in embedded_text
    assert "Uncarded Paper d" in embedded_text
    final_units = json.loads(
        (work_dir / "material_vectors" / "MATERIAL_UNITS_FINAL.json").read_text(
            encoding="utf-8"
        )
    )
    assert {unit["unit_id"] for unit in final_units["units"]} == {
        "u1",
        "u2",
        "u4",
        "u5",
    }
    audit_path = work_dir / "SUPPLEMENTARY_RELEVANCE_FILTER_AUDIT.json"
    assert audit_path.is_file()
    assert json.loads(audit_path.read_text(encoding="utf-8")) == audit


def test_materialize_all_out_of_scope_commits_empty_no_embedding(
    tmp_path,
) -> None:
    units = [
        _filter_unit("u1", "work:off", "Off Paper"),
        _filter_unit("u2", "work:off", "Off Paper"),
    ]
    outcome, work_dir, embed_calls, _materialize, _retrieval, _task = (
        _filter_materialize_fixture(
            tmp_path,
            units=units,
            card_relevance={"work:off": "out_of_scope"},
        )
    )
    audit = outcome.metadata["relevance_filter_audit"]
    assert audit["counts"]["post_filter_unit_count"] == 0
    assert audit["all_out_of_scope"] is True
    assert embed_calls == []
    assert outcome.adequate is True
    assert outcome.total_references == 0
    final_units = json.loads(
        (work_dir / "material_vectors" / "MATERIAL_UNITS_FINAL.json").read_text(
            encoding="utf-8"
        )
    )
    assert final_units["units"] == []
    assert (work_dir / "material_vectors" / "material_vectors.sqlite").is_file()


def test_materialize_missing_card_works_are_retained_fail_safe(
    tmp_path,
) -> None:
    units = [
        _filter_unit("u1", "work:a", "Central Paper a"),
        _filter_unit("u2", "work:no-card", "No Card Paper"),
    ]
    outcome, _work_dir, embed_calls, _materialize, _retrieval, _task = (
        _filter_materialize_fixture(
            tmp_path,
            units=units,
            card_relevance={"work:a": "central"},
        )
    )
    audit = outcome.metadata["relevance_filter_audit"]
    assert audit["unclassified_work_ids"] == ["work:no-card"]
    assert audit["excluded_work_ids"] == []
    assert outcome.total_references == 2
    assert len(embed_calls) == 1
    embedded_text = " ".join(embed_calls[0])
    assert "No Card Paper" in embedded_text


def test_materialize_reuse_preserves_filter_audit_and_counts(tmp_path) -> None:
    units = [
        _filter_unit("u1", "work:a", "Central Paper a"),
        _filter_unit("u2", "work:a", "Central Paper a"),
        _filter_unit("u3", "work:b", "Excluded Paper b"),
        _filter_unit("u4", "work:c", "Contextual Paper c"),
    ]
    first, work_dir, embed_calls, materialize, retrieval, task = (
        _filter_materialize_fixture(
            tmp_path,
            units=units,
            card_relevance={
                "work:a": "central",
                "work:b": "out_of_scope",
                "work:c": "contextual",
            },
        )
    )
    second = materialize(
        task,
        retrieval,
        {},
        _execution_meta(attempt_id="attempt:2"),
    )
    assert second.metadata["reused"] is True
    for key in (
        "relevance_filter_audit",
        "identities",
        "admitted_paper_count",
        "packet_count",
        "card_summary",
        "retained_work_count",
        "retained_unit_count",
    ):
        assert first.metadata[key] == second.metadata[key], key
    assert first.total_references == second.total_references == 3
    assert first.background_only_references == (
        second.background_only_references
    ) == 0
    assert len(embed_calls) == 1
    assert (
        work_dir / "SUPPLEMENTARY_RELEVANCE_FILTER_AUDIT.json"
    ).is_file()
