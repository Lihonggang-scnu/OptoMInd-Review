"""Offline adversarial tests for the isolated R6 regression layer."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from optomind_research.runtime.r6_cross_topic_regression import (
    _classify_scientific_readiness,
    audit_r4_topic,
    audit_r5_topic,
    build_topic_inventory,
    run_r6_cross_topic_regression,
)
from optomind_research.runtime.r6_topic_context_adapter import (
    adapt_topic_context,
    build_live_execution_plan,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_MANIFEST = PROJECT_ROOT / "config" / "R6_TOPIC_MANIFEST.json"


def _missing_historical_r6_assets() -> tuple[Path, ...]:
    """Return legacy fixture paths that were intentionally not migrated."""

    manifest = json.loads(REAL_MANIFEST.read_text(encoding="utf-8"))
    candidates: list[Path] = []
    for topic in manifest.get("topics", []):
        for field in ("r4_root", "r5_root", "phase3_artifacts_root"):
            value = str(topic.get(field) or "").strip()
            if value:
                candidates.append((PROJECT_ROOT / value).resolve())
        for value in topic.get("scoped_kb_paths", []):
            if str(value).strip():
                candidates.append((PROJECT_ROOT / str(value)).resolve())
    return tuple(path for path in candidates if not path.exists())


_HISTORICAL_R6_ASSET_GAPS = _missing_historical_r6_assets()
requires_historical_r6_assets = pytest.mark.skipif(
    bool(_HISTORICAL_R6_ASSET_GAPS),
    reason=(
        "historical R6 output fixtures were intentionally not migrated; "
        "portable behavior is covered by synthetic fixtures"
    ),
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _make_db(path: Path, topic: str, foreign_path: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE papers (paper_id TEXT, title TEXT);
            CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, text TEXT);
            CREATE TABLE visual_chunks (chunk_id TEXT, paper_id TEXT, local_image_path TEXT, caption TEXT);
            """
        )
        connection.execute("INSERT INTO papers VALUES (?, ?)", (f"{topic}-paper-1", topic))
        connection.execute("INSERT INTO text_chunks VALUES (?, ?, ?)", (f"{topic}-text-1", f"{topic}-paper-1", "text"))
        connection.execute(
            "INSERT INTO visual_chunks VALUES (?, ?, ?, ?)",
            (f"{topic}-visual-1", f"{topic}-paper-1", foreign_path or f"assets/{topic}/figure.png", "figure"),
        )
        connection.commit()
    finally:
        connection.close()
    return path


def _make_accepted_r5_root(root: Path, name: str, *, with_plan: bool = True) -> Path:
    r5_root = root / "outputs" / name
    _write_json(
        r5_root / "PROGRAM_FOCUS_GATE.json",
        {
            "schema_version": "research_harness.program_focus_gate.v1",
            "status": "passed",
            "main_problem": {"statement": f"Problem for {name}"},
            "main_hypothesis_ids": [f"H-{name}"],
            "project_type": "simulation",
        },
    )
    if with_plan:
        _write_json(
            r5_root / "RESEARCH_PLAN.json",
            {
                "status": "candidate",
                "verification_status": "verification_deferred",
                "work_packages": [],
            },
        )
    return r5_root


def _make_validated_r5_root(
    root: Path,
    name: str,
    *,
    preserve_r4_candidate_limitations: bool = False,
) -> Path:
    """Create a complete deterministic R5 contract, not merely a focus gate."""
    r5_root = root / "outputs" / name
    opportunity_id = f"OP-{name}"
    hypothesis_id = f"H-{name}"
    _write_json(
        r5_root / "PROGRAM_FOCUS_GATE.json",
        {
            "schema_version": "research_harness.program_focus_gate.v1",
            "status": "passed",
            "main_problem": {"statement": f"Problem for {name}"},
            "selected_opportunity_ids": [opportunity_id],
            "main_hypothesis_ids": [hypothesis_id],
            "future_hypothesis_ids": [f"H-FUTURE-{name}"],
            "future_branches": [
                {
                    "opportunity_id": f"OP-FUTURE-{name}",
                    "hypothesis_id": f"H-FUTURE-{name}",
                }
            ],
            "project_type": "simulation",
        },
    )
    source_limitations = (
        ["R4 candidate retains an unresolved technical item requiring human review."]
        if preserve_r4_candidate_limitations
        else []
    )
    _write_json(
        r5_root / "RESEARCH_PLAN.json",
        {
            "schema_version": "research_harness.research_plan.v2",
            "status": "candidate",
            "main_hypothesis_ids": [hypothesis_id],
            "source_limitations": source_limitations,
            "work_packages": [
                {
                    "work_package_id": "WP01",
                    "opportunity_ids": [opportunity_id],
                    "hypothesis_ids": [hypothesis_id],
                    "verification_status": "verification_deferred",
                }
            ],
            "traceability_matrix": [
                {
                    "opportunity_id": opportunity_id,
                    "hypothesis_id": hypothesis_id,
                    "work_package_id": "WP01",
                }
            ],
            "experiments": [
                {
                    "experiment_id": "EXP01",
                    "hypothesis_ids": [hypothesis_id],
                    "verification_status": "verification_deferred",
                }
            ],
            "expected_results": [
                {
                    "statement": "The planned result remains unexecuted.",
                    "verification_status": "verification_deferred",
                }
            ],
            "results_status": "verification_deferred",
            "verification_deferred": [
                "No experiment, simulation, or data analysis has been executed; all validation is verification_deferred."
            ],
        },
    )
    (r5_root / "RESEARCH_PLAN.md").write_text(
        f"# Research plan for {name}\n\nAll proposed work is verification_deferred.\n",
        encoding="utf-8",
    )
    _write_json(r5_root / "RESEARCH_PLAN_AUDIT.json", {"status": "passed", "errors": []})
    _write_json(r5_root / "RESEARCH_PLAN_CLEANUP_AUDIT.json", {"status": "passed"})
    _write_json(
        r5_root / "RESULT.json",
        {
            "status": "completed",
            "validation_passed": True,
            "stop_reason": "all_gates_passed",
        },
    )
    if preserve_r4_candidate_limitations:
        _write_json(
            r5_root / "PROGRAM_SHARED_CONTEXT.json",
            {"r4_candidate_limitations": [{"flag": "unresolved_technical_item"}]},
        )
    return r5_root


def _synthetic_manifest(root: Path, foreign_path: str | None = None) -> Path:
    topics = []
    for topic_id, category, marker in (
        ("topic_alpha", "fundamental_mechanism", "alpha topic"),
        ("topic_beta", "material_device", "beta topic"),
        ("topic_gamma", "inverse_design", "gamma topic"),
    ):
        db_path = _make_db(
            root / "db" / f"{topic_id}.sqlite",
            topic_id,
            foreign_path if topic_id == "topic_alpha" else None,
        )
        topics.append(
            {
                "topic_id": topic_id,
                "category": category,
                "scientific_scope": marker,
                "path_markers": [marker],
                "r4_root": f"runs/{topic_id}/r4",
                "r5_root": f"runs/{topic_id}/r5",
                "phase3_artifacts_root": f"runs/{topic_id}/phase3",
                "expected_r4_package": "REVIEW_CONTENT_PACKAGE.json",
                "expected_r5_focus_gate": "PROGRAM_FOCUS_GATE.json",
                "scoped_kb_paths": [str(db_path.relative_to(root))],
            }
        )
    manifest = root / "config" / "R6_TOPIC_MANIFEST.json"
    _write_json(
        manifest,
        {
            "schema_version": "research_harness.r6_topic_manifest.v1",
            "manifest_id": "synthetic-r6",
            "project_root": "..",
            "offline_only": True,
            "topics": topics,
        },
    )
    return manifest


@requires_historical_r6_assets
def test_real_manifest_has_three_disjoint_scoped_allowlists():
    manifest = json.loads(REAL_MANIFEST.read_text(encoding="utf-8"))
    inventories = {
        topic["topic_id"]: build_topic_inventory(topic, PROJECT_ROOT)
        for topic in manifest["topics"]
    }
    assert len(inventories) == 3
    assert {topic["category"] for topic in manifest["topics"]} == {
        "fundamental_mechanism",
        "material_device",
        "inverse_design",
    }
    values = list(inventories.values())
    for left_index, left in enumerate(values):
        assert left.paper_ids
        assert left.text_chunk_ids
        assert left.visual_chunk_ids
        assert len(left.report()["allowlists"]["paper"]["fingerprint_sha256"]) == 64
        for right in values[left_index + 1 :]:
            assert not left.paper_ids.intersection(right.paper_ids)
            assert not left.text_chunk_ids.intersection(right.text_chunk_ids)
            assert not left.visual_chunk_ids.intersection(right.visual_chunk_ids)


def test_explicit_r5_root_override_is_resolved_and_audited(tmp_path):
    manifest_path = _synthetic_manifest(tmp_path)
    accepted_root = _make_accepted_r5_root(tmp_path, "accepted_alpha")
    override_path = tmp_path / "r5-overrides.json"
    _write_json(
        override_path,
        {
            "schema_version": "research_harness.r6_r5_root_overrides.v1",
            "overrides": {"topic_alpha": str(accepted_root.relative_to(tmp_path))},
        },
    )

    report = run_r6_cross_topic_regression(
        manifest_path,
        tmp_path / "out",
        r5_overrides_path=override_path,
    )
    resolution = next(item for item in report["r5_root_resolution"] if item["topic_id"] == "topic_alpha")
    assert resolution["source"] == "explicit_override"
    assert resolution["resolved"] == str(accepted_root.resolve())
    assert resolution["program_focus_gate_path"] == str((accepted_root / "PROGRAM_FOCUS_GATE.json").resolve())
    assert resolution["research_plan"]["present"] is True
    assert resolution["research_plan"]["valid_json"] is True
    alpha = next(item for item in report["topics"] if item["topic_id"] == "topic_alpha")
    assert alpha["r5_root_resolution"]["resolved"] == str(accepted_root.resolve())
    assert alpha["r5"]["formal_focus_gate"] is True
    assert report["r5_root_overrides_path"] == str(override_path.resolve())
    assert report["cost"]["r6_cost_cny"] == 0.0


def test_explicit_r5_override_requires_focus_gate(tmp_path):
    manifest_path = _synthetic_manifest(tmp_path)
    missing_gate_root = tmp_path / "outputs" / "missing_gate"
    missing_gate_root.mkdir(parents=True)
    override_path = tmp_path / "missing-gate-overrides.json"
    _write_json(
        override_path,
        {
            "schema_version": "research_harness.r6_r5_root_overrides.v1",
            "overrides": {"topic_alpha": str(missing_gate_root.relative_to(tmp_path))},
        },
    )
    with pytest.raises(ValueError, match="lacks PROGRAM_FOCUS_GATE.json"):
        run_r6_cross_topic_regression(manifest_path, tmp_path / "out", r5_overrides_path=override_path)


def test_explicit_r5_override_rejects_out_of_scope_root(tmp_path):
    manifest_path = _synthetic_manifest(tmp_path)
    outside_root = (tmp_path / ".." / f"outside_{tmp_path.name}" / "accepted").resolve()
    outside_root.mkdir(parents=True)
    _write_json(
        outside_root / "PROGRAM_FOCUS_GATE.json",
        {"status": "passed", "main_problem": {"statement": "outside"}},
    )
    override_path = tmp_path / "scope-overrides.json"
    _write_json(
        override_path,
        {
            "schema_version": "research_harness.r6_r5_root_overrides.v1",
            "overrides": {"topic_alpha": str(outside_root.resolve())},
        },
    )
    with pytest.raises(ValueError, match="outside the project/output scope"):
        run_r6_cross_topic_regression(manifest_path, tmp_path / "out", r5_overrides_path=override_path)


def test_explicit_r5_overrides_reject_root_reuse_across_topics(tmp_path):
    manifest_path = _synthetic_manifest(tmp_path)
    accepted_root = _make_accepted_r5_root(tmp_path, "shared_accepted")
    override_path = tmp_path / "reuse-overrides.json"
    _write_json(
        override_path,
        {
            "schema_version": "research_harness.r6_r5_root_overrides.v1",
            "overrides": {
                "topic_alpha": str(accepted_root.relative_to(tmp_path)),
                "topic_beta": str(accepted_root.relative_to(tmp_path)),
            },
        },
    )
    with pytest.raises(ValueError, match="reused across topics"):
        run_r6_cross_topic_regression(manifest_path, tmp_path / "out", r5_overrides_path=override_path)


@requires_historical_r6_assets
def test_real_offline_replay_is_honest_and_no_external_calls(tmp_path):
    report = run_r6_cross_topic_regression(REAL_MANIFEST, tmp_path / "r6")
    assert report["status"] == "not_ready"
    assert len(report["topics"]) == 3
    assert report["cost"]["r6_cost_cny"] == 0.0
    assert report["cost"]["r6_calls"] == {
        "qwen": 0,
        "semantic_scholar": 0,
        "openalex": 0,
        "downloads": 0,
    }
    by_id = {item["topic_id"]: item for item in report["topics"]}
    assert by_id["mechanism_ep"]["r5"]["formal_focus_gate"] is True
    assert by_id["device_achromatic_metalens"]["r5"]["status"] == "missing_formal_focus_gate"
    assert by_id["inverse_multilayer_ultrafast_mirror"]["r5"]["status"] == "missing_formal_focus_gate"
    assert by_id["device_achromatic_metalens"]["r6_topic_status"] == "ready_for_formal_r5"
    assert by_id["inverse_multilayer_ultrafast_mirror"]["r6_topic_status"] == "ready_for_formal_r5"
    assert by_id["mechanism_ep"]["r4"]["status_classification"] == "candidate_not_failure_or_success"
    assert (tmp_path / "r6" / "R6_CROSS_TOPIC_ACCEPTANCE.json").is_file()
    assert all((tmp_path / "r6" / item / "R6_TOPIC_REPORT.json").is_file() for item in by_id)
    assert (tmp_path / "r6" / "R6_LIVE_EXECUTION_PLAN.json").is_file()
    live_plan = json.loads((tmp_path / "r6" / "R6_LIVE_EXECUTION_PLAN.json").read_text(encoding="utf-8"))
    assert {item["topic_id"] for item in live_plan["topics"]} == {
        "device_achromatic_metalens",
        "inverse_multilayer_ultrafast_mirror",
    }
    assert all(item["enabled"] is False for item in live_plan["topics"])
    assert all(item["hard_cost_cap_cny"] == 2.0 for item in live_plan["topics"])
    assert all(item["token_budget"] == 120000 for item in live_plan["topics"])
    assert all(item["max_iters"] == 8 for item in live_plan["topics"])
    assert report["live_execution_plan"]["manifest_topic_count"] == 3
    assert report["live_execution_plan"]["command_topic_count"] == 2
    assert report["live_execution_plan"]["actionable_topic_count"] == 2
    assert report["live_execution_plan"]["topic_count"] == 2
    assert report["live_execution_plan"]["topic_count_semantics"] == "deprecated_alias_of_command_topic_count"
    assert live_plan["manifest_topic_count"] == 3
    assert live_plan["command_topic_count"] == 2
    assert live_plan["actionable_topic_count"] == 2
    assert live_plan["topic_count_semantics"] == "deprecated_alias_of_command_topic_count"
    for topic_id in by_id:
        topic_output = tmp_path / "r6" / topic_id
        context = json.loads((topic_output / "R6_TOPIC_CONTEXT.json").read_text(encoding="utf-8"))
        relation = json.loads((topic_output / "RELATION_GRAPH.json").read_text(encoding="utf-8"))
        scope = json.loads((topic_output / "REVIEW_SCOPE_MAP.json").read_text(encoding="utf-8"))
        expected_relation_status = (
            "empty_verified"
            if context["phase3_contract"]["status"] == "available" and context["discovered_inputs"]["relation_graphs"]
            else "empty_unavailable"
        )
        assert relation["status"] == expected_relation_status
        assert relation["nodes"] == []
        assert relation["relations"] == []
        assert relation["node_count"] == 0
        assert relation["relation_count"] == 0
        assert scope["claim_count"] == 0
        assert scope["relation_count"] == 0
        assert all(section["claims"] == [] and section["evidence_ids"] == [] for section in scope["sections"])
        assert context["scientific_content_policy"]["synthetic_claims_created"] == 0
        assert context["scientific_content_policy"]["synthetic_relations_created"] == 0
    metalens_plan = next(item for item in live_plan["topics"] if item["topic_id"] == "device_achromatic_metalens")
    metalens_context = json.loads(
        (tmp_path / "r6" / "device_achromatic_metalens" / "R6_TOPIC_CONTEXT.json").read_text(encoding="utf-8")
    )
    legacy_base = metalens_context["declared_package_paths"]["base_kb_sqlite"]["path"]
    assert metalens_context["candidate_constraints"]["legacy_package_base_kb_is_unscoped"] is False
    assert legacy_base in metalens_plan["command"]
    assert "cross_topic_regression_20260712\\achromatic_metalens\\review_kb\\review_knowledge_base.sqlite" in metalens_plan["command"]
    ep_context = json.loads(
        (tmp_path / "r6" / "mechanism_ep" / "R6_TOPIC_CONTEXT.json").read_text(encoding="utf-8")
    )
    ep_legacy_base = ep_context["declared_package_paths"]["base_kb_sqlite"]["path"]
    assert ep_context["candidate_constraints"]["legacy_package_base_kb_is_unscoped"] is True
    assert all(ep_legacy_base not in item["command"] for item in live_plan["topics"])
    ep_limitations = json.loads(
        (tmp_path / "r6" / "mechanism_ep" / "LEGACY_R4_HANDOFF_LIMITATIONS.json").read_text(encoding="utf-8")
    )
    assert "current_phase3_claims_missing" not in {item["code"] for item in ep_limitations["limitations"]}
    assert "current_phase3_relations_missing" not in {item["code"] for item in ep_limitations["limitations"]}
    metalens_limitations = json.loads(
        (tmp_path / "r6" / "device_achromatic_metalens" / "LEGACY_R4_HANDOFF_LIMITATIONS.json").read_text(encoding="utf-8")
    )
    assert {"current_phase3_claims_missing", "current_phase3_relations_missing"}.issubset(
        {item["code"] for item in metalens_limitations["limitations"]}
    )


def _real_topic(topic_id: str) -> dict:
    manifest = json.loads(REAL_MANIFEST.read_text(encoding="utf-8"))
    return next(topic for topic in manifest["topics"] if topic["topic_id"] == topic_id)


@requires_historical_r6_assets
def test_multilayer_wrong_topic_visual_is_omitted_and_full_quality_report_is_routed(tmp_path):
    topic = _real_topic("inverse_multilayer_ultrafast_mirror")
    context = adapt_topic_context(topic, PROJECT_ROOT, output_dir=tmp_path / "adapter")
    plan = build_live_execution_plan({"topics": [topic]}, [context], PROJECT_ROOT)
    item = plan["topics"][0]
    routing = item["visual_plan_routing"]
    quality_path = PROJECT_ROOT / "outputs/research_harness_e2e/high_power_multilayer_real_v2_20260727/REVIEW_HARNESS_QUALITY_REPORT.json"
    technical_path = PROJECT_ROOT / "outputs/research_harness_e2e/high_power_multilayer_real_v2_20260727/authoring/full_review/FULL_REVIEW_PACKAGE.json"

    assert routing["omitted"] is True
    assert routing["selected_path"] is None
    assert "quality_report:visual_plan_topic_identity_mismatch" in routing["omission_reasons"]
    assert "--visual-plan" not in item["command"]
    assert item["quality_report_path"] == str(quality_path.resolve())
    assert item["technical_audit_path"] == str(technical_path.resolve())
    assert "--quality-report" in item["command"]
    assert str(quality_path.resolve()) in item["command"]
    assert "--technical-audit" in item["command"]
    assert str(technical_path.resolve()) in item["command"]
    assert plan["external_calls_made"] == {"qwen": 0, "semantic_scholar": 0, "downloads": 0}


@requires_historical_r6_assets
def test_multilayer_ledger_ids_absent_from_scoped_sqlite_are_unavailable(tmp_path):
    topic = _real_topic("inverse_multilayer_ultrafast_mirror")
    context = adapt_topic_context(topic, PROJECT_ROOT, output_dir=tmp_path / "adapter")
    artifact = json.loads(
        (tmp_path / "adapter" / "R6_SOURCE_PERMISSIONS_ADAPTER.json").read_text(encoding="utf-8")
    )
    reconciliation = artifact["ledger_inventory_reconciliation"]

    assert len(reconciliation["missing_ledger_paper_ids"]) == 3
    assert len(reconciliation["missing_ledger_chunk_ids"]) == 5
    assert reconciliation["available_chunk_count"] == reconciliation["ledger_chunk_count"] - 5
    assert reconciliation["no_rows_fabricated"] is True
    for chunk_id in reconciliation["missing_ledger_chunk_ids"]:
        row = artifact["chunk_permissions"][chunk_id]
        assert row["present_in_scoped_kb"] is False
        assert row["permission"] == "unavailable"
        assert chunk_id not in artifact["factual_support_chunk_ids"]
        assert chunk_id in artifact["pruned_from_factual_support_chunk_ids"]
    for paper_id in reconciliation["missing_ledger_paper_ids"]:
        row = artifact["paper_permissions"][paper_id]
        assert row["valid_in_scoped_kb"] is False
        assert row["permission"] == "discovery_only"
    assert context["ledger_inventory_reconciliation"] == reconciliation


@requires_historical_r6_assets
def test_multilayer_authoritative_permission_counts_are_nonempty_and_conservative(tmp_path):
    topic = _real_topic("inverse_multilayer_ultrafast_mirror")
    context = adapt_topic_context(topic, PROJECT_ROOT, output_dir=tmp_path / "adapter")
    artifact = json.loads(
        (tmp_path / "adapter" / "R6_SOURCE_PERMISSIONS_ADAPTER.json").read_text(encoding="utf-8")
    )
    counts = artifact["permission_counts"]

    assert artifact["policy"]["authoritative_for_r6"] is True
    assert counts
    assert sum(counts.values()) == len(artifact["chunk_permissions"])
    assert artifact["paper_permission_counts"]
    assert artifact["policy"]["absent_rows_can_support_facts"] is False
    assert artifact["policy"]["discovery_only_can_support_facts"] is False
    assert not set(artifact["missing_ledger_chunks"]).intersection(artifact["factual_support_chunk_ids"])
    assert context["source_permission_audit"]["authoritative_adapter_path"]


def test_foreign_visual_path_fails_scope_isolation(tmp_path):
    manifest = _synthetic_manifest(tmp_path, foreign_path="assets/topic_beta/figure.png")
    report = run_r6_cross_topic_regression(manifest, tmp_path / "out")
    assert report["status"] == "failed"
    alpha = next(item for item in report["topics"] if item["topic_id"] == "topic_alpha")
    assert alpha["cross_topic_path_hits"]
    assert alpha["r6_topic_status"] == "failed_scope_isolation"


def test_discovery_only_cannot_support_facts(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    r5_root = root / "r5"
    _write_json(r5_root / "PROGRAM_FOCUS_GATE.json", {"status": "passed", "main_problem": {"statement": "p"}, "main_hypothesis_ids": ["H1"]})
    _write_json(
        r5_root / "RESEARCH_PLAN.json",
        {"premise": {"permission": "discovery_only", "supports_claim": True, "fact": "forbidden"}},
    )
    topic = {
        "topic_id": "synthetic",
        "r5_root": "r5",
        "expected_r5_focus_gate": "PROGRAM_FOCUS_GATE.json",
    }
    result = audit_r5_topic(topic, root, {"status": "completed"})
    assert result["discovery_only_fact_support"]


def test_future_branch_cannot_leak_into_work_packages(tmp_path):
    root = tmp_path / "project"
    r5_root = _make_validated_r5_root(root, "future_leak")
    gate = json.loads((r5_root / "PROGRAM_FOCUS_GATE.json").read_text(encoding="utf-8"))
    plan = json.loads((r5_root / "RESEARCH_PLAN.json").read_text(encoding="utf-8"))
    future_id = gate["future_hypothesis_ids"][0]
    plan["work_packages"][0]["hypothesis_ids"].append(future_id)
    _write_json(r5_root / "RESEARCH_PLAN.json", plan)
    topic = {"topic_id": "synthetic", "r5_root": str(r5_root.relative_to(root))}
    result = audit_r5_topic(topic, root, {"status": "completed"})
    assert result["future_branch_leakage"]
    assert "future_branch_leakage" in result["durable_plan_contract"]["blocking_reasons"]
    assert result["validated_completed_plan"] is False


def test_verification_deferred_is_recorded_but_not_a_violation(tmp_path):
    root = tmp_path / "project"
    r5_root = _make_validated_r5_root(root, "deferred")
    result = audit_r5_topic(
        {"topic_id": "synthetic", "r5_root": str(r5_root.relative_to(root))},
        root,
        {"status": "completed"},
    )
    assert result["verification_deferred"]
    assert result["verification_violations"] == []
    assert result["durable_plan_contract"]["verification_contract"]["passed"] is True
    assert result["validated_completed_plan"] is True


def test_global_deferred_contract_allows_unmarked_planned_experiments_and_outcomes(tmp_path):
    root = tmp_path / "project"
    r5_root = _make_validated_r5_root(root, "global_deferred")
    plan_path = r5_root / "RESEARCH_PLAN.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan.pop("verification_deferred", None)
    plan["experiments"] = [
        "Measure the proposed device response against the declared baseline.",
        "Test robustness under the planned perturbation sweep.",
    ]
    plan["work_packages"][0]["stop_or_pivot_criteria"] = [
        "Stop if the proposed method shows no statistically significant improvement over the baseline."
    ]
    plan["expected_results"] = [
        "The proposed design is expected to improve the target metric.",
        "A statistically significant improvement is expected if the hypothesis is correct.",
    ]
    _write_json(plan_path, plan)

    result = audit_r5_topic(
        {"topic_id": "synthetic", "r5_root": str(r5_root.relative_to(root))},
        root,
        {"status": "completed"},
    )
    contract = result["durable_plan_contract"]["verification_contract"]
    assert contract["results_status"] == "verification_deferred"
    assert contract["work_packages_deferred"] is True
    assert contract["executed_result_violations"] == []
    assert contract["passed"] is True
    assert result["validated_completed_plan"] is True


def test_global_deferred_contract_blocks_fake_executed_result_language(tmp_path):
    root = tmp_path / "project"
    r5_root = _make_validated_r5_root(root, "fake_executed_result")
    plan_path = r5_root / "RESEARCH_PLAN.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["expected_results"] = [
        "We measured a lower error, and the results show a statistically significant improvement."
    ]
    _write_json(plan_path, plan)

    result = audit_r5_topic(
        {"topic_id": "synthetic", "r5_root": str(r5_root.relative_to(root))},
        root,
        {"status": "completed"},
    )
    contract = result["durable_plan_contract"]["verification_contract"]
    assert contract["passed"] is False
    assert any("first_person_past_result" in item for item in contract["executed_result_violations"])
    assert any("direct_results_claim" in item for item in contract["executed_result_violations"])
    assert "verification_contract_failed" in result["durable_plan_contract"]["blocking_reasons"]
    assert result["validated_completed_plan"] is False


def test_r4_candidate_limitations_are_preserved_in_r5_context(tmp_path):
    root = tmp_path / "project"
    r5_root = _make_validated_r5_root(
        root,
        "candidate_limitations",
        preserve_r4_candidate_limitations=True,
    )
    result = audit_r5_topic(
        {"topic_id": "synthetic", "r5_root": str(r5_root.relative_to(root))},
        root,
        {"status": "awaiting_human_review"},
    )
    limitations = result["candidate_limitations"]
    assert limitations["r4_candidate"] is True
    assert limitations["limitation_count"] >= 1
    assert limitations["preserved"] is True
    assert limitations["sources"]["program_shared_context"] == 1
    assert result["validated_completed_plan"] is True


def test_passed_focus_without_durable_plan_is_not_complete(tmp_path):
    root = tmp_path / "project"
    r5_root = _make_accepted_r5_root(root, "focus_only", with_plan=False)
    result = audit_r5_topic(
        {"topic_id": "synthetic", "r5_root": str(r5_root.relative_to(root))},
        root,
        {"status": "completed"},
    )
    blockers = result["durable_plan_contract"]["blocking_reasons"]
    assert result["formal_focus_passed"] is True
    assert result["validated_completed_plan"] is False
    assert "research_plan_json_missing_or_invalid" in blockers
    assert "research_plan_markdown_missing_or_empty" in blockers


def test_passed_plan_audit_with_waiting_result_is_not_complete(tmp_path):
    root = tmp_path / "project"
    r5_root = _make_validated_r5_root(root, "waiting_result")
    _write_json(
        r5_root / "RESULT.json",
        {
            "status": "waiting_for_human",
            "validation_passed": False,
            "stop_reason": "awaiting_human_review",
        },
    )
    result = audit_r5_topic(
        {"topic_id": "synthetic", "r5_root": str(r5_root.relative_to(root))},
        root,
        {"status": "completed"},
    )
    blockers = result["durable_plan_contract"]["blocking_reasons"]
    assert result["validated_completed_plan"] is False
    assert "result_status_not_completed" in blockers
    assert "result_validation_not_passed" in blockers
    assert "result_stop_reason_not_canonical_success" in blockers


@pytest.mark.parametrize(
    "stop_reason",
    ["all_gates_passed", "deterministic_post_validation_passed"],
)
def test_canonical_successful_r5_stop_reasons_are_accepted(tmp_path, stop_reason):
    root = tmp_path / "project"
    r5_root = _make_validated_r5_root(root, f"successful_{stop_reason}")
    _write_json(
        r5_root / "RESULT.json",
        {
            "status": "completed",
            "validation_passed": True,
            "stop_reason": stop_reason,
        },
    )
    result = audit_r5_topic(
        {"topic_id": "synthetic", "r5_root": str(r5_root.relative_to(root))},
        root,
        {"status": "completed"},
    )
    result_artifact = result["durable_plan_contract"]["artifacts"]["result"]
    assert result["validated_completed_plan"] is True
    assert result_artifact["successful_stop_reason"] is True
    assert set(result_artifact["accepted_stop_reasons"]) == {
        "all_gates_passed",
        "deterministic_post_validation_passed",
    }


def test_other_completed_r5_stop_reason_is_rejected(tmp_path):
    root = tmp_path / "project"
    r5_root = _make_validated_r5_root(root, "noncanonical_stop")
    _write_json(
        r5_root / "RESULT.json",
        {
            "status": "completed",
            "validation_passed": True,
            "stop_reason": "completed_after_manual_patch",
        },
    )
    result = audit_r5_topic(
        {"topic_id": "synthetic", "r5_root": str(r5_root.relative_to(root))},
        root,
        {"status": "completed"},
    )
    assert result["validated_completed_plan"] is False
    assert "result_stop_reason_not_canonical_success" in result["durable_plan_contract"]["blocking_reasons"]
    assert result["durable_plan_contract"]["artifacts"]["result"]["successful_stop_reason"] is False


def test_completed_result_with_failed_plan_audit_is_not_complete(tmp_path):
    root = tmp_path / "project"
    r5_root = _make_validated_r5_root(root, "failed_audit")
    _write_json(
        r5_root / "RESEARCH_PLAN_AUDIT.json",
        {"status": "failed", "errors": ["traceability broken"]},
    )
    result = audit_r5_topic(
        {"topic_id": "synthetic", "r5_root": str(r5_root.relative_to(root))},
        root,
        {"status": "completed"},
    )
    assert result["validated_completed_plan"] is False
    assert "research_plan_audit_not_passed" in result["durable_plan_contract"]["blocking_reasons"]
    assert result["durable_plan_contract"]["artifacts"]["research_plan_audit"]["errors"]


def test_fully_valid_durable_plan_contract_passes(tmp_path):
    root = tmp_path / "project"
    r5_root = _make_validated_r5_root(root, "complete")
    result = audit_r5_topic(
        {"topic_id": "synthetic", "r5_root": str(r5_root.relative_to(root))},
        root,
        {"status": "completed"},
    )
    contract = result["durable_plan_contract"]
    assert result["formal_focus_passed"] is True
    assert result["validated_completed_plan"] is True
    assert contract["status"] == "passed"
    assert contract["blocking_reasons"] == []
    assert contract["artifacts"]["research_plan_json"]["path"] == str(r5_root / "RESEARCH_PLAN.json")
    assert contract["artifacts"]["research_plan_markdown"]["present"] is True


def test_r5_phase_accounting_prefers_lifetime_and_marks_v1_ambiguity(tmp_path):
    root = tmp_path / "project"
    v2_root = _make_validated_r5_root(root, "accounting_v2")
    _write_json(
        v2_root / "R5_PHASE_ACCOUNTING.json",
        {
            "schema_version": "research_harness.r5_phase_accounting.v2",
            "lifetime_total": {
                "model_calls": 9,
                "input_tokens": 1234,
                "output_tokens": 234,
                "estimated_cost_cny": 1.25,
                "wall_time_seconds": 45,
            },
        },
    )
    _write_json(v2_root / "COST.json", {"estimated_cost_cny": 99.0, "input_tokens": 999999})
    v2 = audit_r5_topic(
        {"topic_id": "v2", "r5_root": str(v2_root.relative_to(root))},
        root,
        {"status": "completed"},
    )["cost"]
    assert v2["estimated_cost_cny"] == 1.25
    assert v2["input_tokens"] == 1234
    assert v2["source_kind"] == "r5_phase_accounting_lifetime_total"
    assert v2["historical_ambiguity"] is False

    v1_root = _make_validated_r5_root(root, "accounting_v1")
    repeated = {
        "run_id": "resume-1",
        "model_calls": 2,
        "input_tokens": 100,
        "output_tokens": 20,
        "estimated_cost_cny": 0.5,
    }
    _write_json(
        v1_root / "R5_PHASE_ACCOUNTING.json",
        {
            "schema_version": "research_harness.r5_phase_accounting.v1",
            "phases": {
                "focus": {"runs": [repeated]},
                "resume": {
                    "runs": [
                        repeated,
                        {
                            "run_id": "resume-2",
                            "model_calls": 1,
                            "input_tokens": 50,
                            "output_tokens": 10,
                            "estimated_cost_cny": 0.25,
                        },
                    ]
                },
            },
        },
    )
    v1 = audit_r5_topic(
        {"topic_id": "v1", "r5_root": str(v1_root.relative_to(root))},
        root,
        {"status": "completed"},
    )["cost"]
    assert v1["estimated_cost_cny"] == pytest.approx(0.75)
    assert v1["input_tokens"] == 150
    assert v1["historical_ambiguity"] is True
    assert "lifetime_total_missing" in v1["ambiguity_reasons"]
    assert v1["deduplication"]["duplicate_run_ids"] == ["resume-1"]


def test_strict_r6_requires_three_validated_completed_plans(tmp_path):
    manifest_path = _synthetic_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    overrides: dict[str, str] = {}
    roots: dict[str, Path] = {}
    for topic in manifest["topics"]:
        topic_id = topic["topic_id"]
        _write_json(
            tmp_path / topic["r4_root"] / "REVIEW_CONTENT_PACKAGE.json",
            {"status": "completed", "word_count": 500},
        )
        roots[topic_id] = _make_validated_r5_root(tmp_path, f"strict_{topic_id}")
        overrides[topic_id] = str(roots[topic_id].relative_to(tmp_path))
    override_path = tmp_path / "strict-overrides.json"
    _write_json(
        override_path,
        {
            "schema_version": "research_harness.r6_r5_root_overrides.v1",
            "overrides": overrides,
        },
    )

    passed = run_r6_cross_topic_regression(
        manifest_path,
        tmp_path / "strict-pass",
        r5_overrides_path=override_path,
        strict=True,
    )
    assert passed["status"] == "passed"
    assert passed["engineering_contract"]["status"] == "passed"
    assert passed["engineering_contract"]["passed"] is True
    assert passed["scientific_readiness"]["classification"] == "not_evidence_complete"
    assert passed["scientific_readiness"]["all_topics_evidence_complete"] is False
    assert passed["acceptance_gates"]["all_topics_validated_completed_r5_plan"] is True
    assert passed["acceptance_gates"]["strict_acceptance_passed"] is True

    (roots["topic_gamma"] / "RESEARCH_PLAN.md").unlink()
    failed = run_r6_cross_topic_regression(
        manifest_path,
        tmp_path / "strict-fail",
        r5_overrides_path=override_path,
        strict=True,
    )
    assert failed["status"] == "not_ready"
    assert failed["acceptance_gates"]["all_topics_validated_completed_r5_plan"] is False
    assert failed["acceptance_gates"]["strict_acceptance_passed"] is False
    gamma = next(item for item in failed["topics"] if item["topic_id"] == "topic_gamma")
    assert "research_plan_markdown_missing_or_empty" in gamma["r5"]["durable_plan_contract"]["blocking_reasons"]


def test_scientific_readiness_separates_ep_like_block_from_complete_topic():
    blocked = _classify_scientific_readiness(
        {
            "status": "awaiting_human_review",
            "status_classification": "candidate_not_failure_or_success",
        },
        {
            "candidate_limitations": {
                "r4_candidate": True,
                "limitation_count": 3,
                "preserved": True,
            }
        },
        {
            "preflight": {
                "status": "blocked_honest_stop",
                "mandatory_missing": ["blueprint"],
            },
            "source_permission_audit": {
                "counts": {"qualified_only": 20, "unavailable": 2},
                "ledger_inventory_reconciliation": {
                    "missing_ledger_chunk_ids": ["missing-chunk"],
                    "missing_ledger_paper_ids": [],
                    "db_read_errors": [],
                },
            },
        },
    )
    assert blocked["classification"] == "blocked_context"
    assert blocked["evidence_complete"] is False
    assert "context_preflight_blocked" in blocked["reasons"]
    assert "no_direct_factual_support_permission" in blocked["reasons"]

    complete = _classify_scientific_readiness(
        {"status": "completed", "status_classification": "completed_candidate"},
        {
            "candidate_limitations": {
                "r4_candidate": False,
                "limitation_count": 0,
                "preserved": True,
            }
        },
        {
            "preflight": {
                "status": "ready_for_r5_context_consumption",
                "mandatory_missing": [],
            },
            "source_permission_audit": {
                "counts": {"factual_support": 24, "qualified_only": 5},
                "ledger_inventory_reconciliation": {
                    "missing_ledger_chunk_ids": [],
                    "missing_ledger_paper_ids": [],
                    "db_read_errors": [],
                },
            },
        },
    )
    assert complete["classification"] == "evidence_ready"
    assert complete["evidence_complete"] is True
    assert complete["reasons"] == []


def test_legacy_context_adapter_stops_honestly_without_phase3_claims_or_relations(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    db = _make_db(root / "db" / "topic.sqlite", "topic")
    topic = {
        "topic_id": "topic",
        "category": "material_device",
        "scientific_scope": "a test topic",
        "r4_root": "r4",
        "r5_root": "r5",
        "phase3_artifacts_root": "coverage",
        "scoped_kb_paths": [str(db.relative_to(root))],
    }
    context = adapt_topic_context(topic, root)
    assert context["read_only"] is True
    assert context["preflight"]["status"] == "blocked_honest_stop"
    assert "current_phase3_claim_contract_unavailable_legacy_assets_only" in context["preflight"]["nonblocking_limitations"]
    assert "current_phase3_contract" not in context["preflight"]["mandatory_missing"]
    assert context["scientific_content_policy"]["synthetic_claims_created"] == 0
    assert context["scientific_content_policy"]["synthetic_relations_created"] == 0
    assert context["legacy_to_current_policy"]["never_invent_phase3_claims_or_relations"] is True


def test_legacy_adapter_allows_core_context_without_phase3_or_coverage_root(tmp_path):
    root = tmp_path / "project"
    r4_root = root / "r4"
    _write_json(
        r4_root / "REVIEW_BLUEPRINT.json",
        {
            "user_question": "How do optical devices control broadband dispersion?",
            "sections": [
                {
                    "section_id": "S01",
                    "title": "Mechanisms",
                    "argument_role": "Explain the governing mechanism",
                }
            ],
        },
    )
    (r4_root / "FINAL_REVIEW_EN.md").parent.mkdir(parents=True, exist_ok=True)
    (r4_root / "FINAL_REVIEW_EN.md").write_text("A durable review candidate.", encoding="utf-8")
    _write_json(r4_root / "SOURCE_PERMISSIONS.json", {"permissions": []})
    _write_json(r4_root / "TECHNICAL_AUDIT.json", {"status": "reviewed"})
    db = _make_db(root / "db" / "topic.sqlite", "topic")
    topic = {
        "topic_id": "topic",
        "category": "material_device",
        "r4_root": "r4",
        "scoped_kb_paths": [str(db.relative_to(root))],
    }

    context = adapt_topic_context(topic, root, output_dir=root / "adapter")
    assert context["preflight"]["status"] == "ready_for_r5_context_consumption"
    assert context["preflight"]["mandatory_missing"] == []
    assert "current_phase3_claim_contract_unavailable_legacy_assets_only" in context["preflight"]["nonblocking_limitations"]
    assert "coverage_root_unavailable_r5_may_need_compatibility_input" in context["preflight"]["nonblocking_limitations"]
    assert context["phase3_contract"]["status"] != "available"
    assert context["scientific_content_policy"]["synthetic_claims_created"] == 0
    assert context["scientific_content_policy"]["synthetic_relations_created"] == 0

    relation = json.loads((root / "adapter" / "RELATION_GRAPH.json").read_text(encoding="utf-8"))
    assert relation["status"] == "empty_unavailable"
    assert relation["relations"] == []
    assert relation["nodes"] == []
    scope = json.loads((root / "adapter" / "REVIEW_SCOPE_MAP.json").read_text(encoding="utf-8"))
    assert scope["sections"][0]["section_id"] == "S01"
    assert scope["sections"][0]["claims"] == []
    assert scope["sections"][0]["evidence_ids"] == []


def test_live_plan_uses_scoped_kb_and_hard_cap_without_execution(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    db = _make_db(root / "db" / "topic.sqlite", "topic")
    topic = {
        "topic_id": "topic",
        "category": "material_device",
        "scoped_kb_paths": [str(db.relative_to(root))],
        "r4_root": "r4",
        "r5_root": "r5",
        "phase3_artifacts_root": "coverage",
        "live_r5_plan": {
            "enabled": False,
            "hard_cost_cap_cny": 1.75,
            "token_budget": 50000,
            "max_iters": 4,
        },
    }
    context = adapt_topic_context(topic, root)
    plan = build_live_execution_plan({"topics": [topic]}, [context], root)
    item = plan["topics"][0]
    assert item["execution_status"] == "blocked_preflight"
    assert item["hard_cost_cap_cny"] == 1.75
    assert str(db) in item["command"]
    assert "--resume-plan-only" in item["resume_plan_only_command"]
    assert plan["external_calls_made"] == {"qwen": 0, "semantic_scholar": 0, "downloads": 0}


def test_awaiting_r4_status_is_not_reclassified_and_cost_reuse_are_reported(tmp_path):
    root = tmp_path / "project"
    r4_root = root / "r4"
    _write_json(
        r4_root / "REVIEW_CONTENT_PACKAGE.json",
        {
            "status": "awaiting_human_review",
            "total_cost_cny": 1.25,
            "total_input_tokens": 100,
            "total_output_tokens": 20,
            "reused": True,
        },
    )
    result = audit_r4_topic(
        {"topic_id": "synthetic", "r4_root": "r4", "expected_r4_package": "REVIEW_CONTENT_PACKAGE.json"},
        root,
    )
    assert result["status_classification"] == "candidate_not_failure_or_success"
    assert result["cost"]["estimated_cost_cny"] == 1.25
    assert result["reuse_count"] >= 1


@pytest.mark.parametrize("status", ["needs_more_literature", "awaiting_human_review", "partial"])
def test_nonterminal_statuses_are_explicitly_nonterminal(tmp_path, status):
    root = tmp_path / "project"
    _write_json(root / "r4" / "REVIEW_CONTENT_PACKAGE.json", {"status": status})
    result = audit_r4_topic(
        {"topic_id": "synthetic", "r4_root": "r4", "expected_r4_package": "REVIEW_CONTENT_PACKAGE.json"},
        root,
    )
    assert result["status_classification"] == "candidate_not_failure_or_success"
