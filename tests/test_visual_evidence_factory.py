from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import types
import uuid
from pathlib import Path

import pytest
from PIL import Image

from optomind_research.runtime.latex_publication_renderer import (
    _copy_verified_figures,
)
from optomind_research.runtime.conceptual_diagram_renderer import (
    ConceptualDiagramRenderer,
)
from optomind_research.runtime.visual_evidence_factory import (
    VisualEvidenceFactory,
    VisualEvidenceFactoryConfig,
    apply_structural_visual_policy,
    build_source_audit_cache_key,
    build_visual_factory_input_fingerprint,
    derive_visual_cache_namespace,
    normalize_visual_factory_plan,
    scoped_visual_cache_dir,
    validate_final_visual_package_file,
)
from optomind_research.runtime.visual_editor_tool_provider import (
    VisualEditorContext,
    VisualEditorToolProvider,
    validate_visual_editorial_plan_file,
)
from optomind_research.runtime.visual_review_queue import (
    apply_visual_review_queue,
    build_visual_review_queue,
)
from optomind_research.conceptual_visual_generator import (
    ConceptualVisualGenerator,
)

_TEMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp-visual-factory-tests"


@pytest.fixture()
def tmp_path() -> Path:
    """Workspace-local tmp dir; sandbox denies pytest's default temp ACLs."""

    _TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TEMP_ROOT / f"run-{uuid.uuid4().hex[:12]}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
        try:
            _TEMP_ROOT.rmdir()
        except OSError:
            pass


def _image(path: Path, color: str = "navy") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (360, 240), color=color).save(path)
    return path


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _blueprint() -> dict:
    return {
        "review_thesis": "Optical resonances connect mechanism and sensing.",
        "sections": [
            {
                "section_id": "S01",
                "title": "Resonant mechanism",
                "full_text": (
                    "Optical resonances connect mechanism and sensing through "
                    "field confinement in the resonant cavity."
                ),
                "argument_role": (
                    "Explain optical resonance, field confinement, and sensing."
                ),
                "visual_argument_slots": [
                    {"purpose": "Explain the resonant sensing mechanism."}
                ],
            }
        ],
    }


def test_pending_current_topic_visual_is_retrievable(tmp_path: Path):
    image_path = _image(tmp_path / "pending.png")
    kb_path = tmp_path / "supplemental.sqlite"
    conn = sqlite3.connect(str(kb_path))
    try:
        conn.execute(
            """
            CREATE TABLE visual_chunks(
              chunk_id TEXT PRIMARY KEY,
              paper_id TEXT,
              doi TEXT,
              title TEXT,
              caption TEXT,
              chunk_kind TEXT,
              search_text TEXT,
              visual_argument_type TEXT,
              visual_argument_status TEXT,
              visual_argument_confidence TEXT,
              visual_argument_claim TEXT,
              visual_argument_needs_human_review INTEGER,
              visual_argument_schema_version TEXT,
              local_image_path TEXT,
              raw_json TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO visual_chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "vis-pending-1",
                "paper-current",
                "10.1/current",
                "Current optical resonator sensing study",
                "Resonant mode confinement and spectral sensing response.",
                "single_figure",
                "optical resonance field confinement sensing mechanism",
                "mechanism_anchor",
                "pending_multimodal_review",
                "medium",
                "",
                1,
                "test",
                str(image_path),
                "{}",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    provider = VisualEditorToolProvider(
        VisualEditorContext(
            blueprint=_blueprint(),
            review_work_dir=tmp_path / "review",
            work_dir=tmp_path / "visual",
            kb_sqlite_paths=[kb_path],
        )
    )
    candidates = provider._verified_candidates_for_section(
        "S01",
        top_k=4,
    )
    assert [row["chunk_id"] for row in candidates] == [
        "vis-pending-1"
    ]
    assert candidates[0]["visual_argument_status"] == (
        "pending_multimodal_review"
    )


def test_editorial_plan_accepts_traceable_pending_source(tmp_path: Path):
    image_path = _image(tmp_path / "pending.png")
    plan_path = tmp_path / "VISUAL_EDITORIAL_PLAN.json"
    _write_json(
        plan_path,
        {
            "placements": [
                {
                    "visual_chunk_id": "pending-1",
                    "paper_id": "paper-1",
                    "local_image_path": str(image_path),
                    "status": "traceable_source_pending_review",
                }
            ],
            "conceptual_figure_requests": [],
            "unfilled_visual_needs": [],
        },
    )
    assert validate_visual_editorial_plan_file(plan_path).startswith(
        "VALIDATION_PASSED"
    )


def test_factory_materializes_source_figure_in_test_mode(tmp_path: Path):
    image_path = _image(tmp_path / "source.png")
    plan_path = tmp_path / "VISUAL_EDITORIAL_PLAN.json"
    _write_json(
        plan_path,
        {
            "placements": [
                {
                    "section_id": "S01",
                    "visual_chunk_id": "vis-1",
                    "paper_id": "paper-1",
                    "doi": "10.1/source",
                    "local_image_path": str(image_path),
                    "caption_preview": "Measured resonant sensing response.",
                    "argumentative_purpose": (
                        "Show how field confinement connects to sensing."
                    ),
                    "placement_guidance": "S01",
                    "status": "traceable_source_pending_review",
                }
            ],
            "conceptual_figure_requests": [],
            "unfilled_visual_needs": [],
        },
    )
    factory = VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=tmp_path / "factory",
            real_visual_audit=False,
            real_image_generation=False,
            test_mode=True,
        )
    )
    package = factory.run(
        visual_plan_path=plan_path,
        blueprint=_blueprint(),
        review_work_dir=tmp_path / "review",
    )
    assert package["validation"]["status"] == "passed"
    assert len(package["figures"]) == 1
    figure = package["figures"][0]
    assert Path(figure["local_path"]).is_file()
    assert figure["review_decision"] == (
        "system_approved_test_mode_with_warnings"
    )
    assert figure["source_route"] == "source_derived"
    assert figure["panel_manifest"][0]["paper_id"] == "paper-1"


def test_factory_reads_staged_remount_reference_without_materializing_requests(
    tmp_path: Path,
):
    image_path = _image(tmp_path / "source-remount.png")
    wrapped_plan = {
        "schema_version": (
            "optomind.staged_visual_remount.v1."
            "factory_plan_reference.v1"
        ),
        "content_fingerprint": "remount-fixture",
        "editorial_plan": {
            "schema_version": "research_harness.visual_editorial_plan.v1",
            "placements": [
                {
                    "section_id": "S01",
                    "visual_chunk_id": "vis-remount-1",
                    "paper_id": "paper-remount-1",
                    "doi": "10.1/remount",
                    "local_image_path": str(image_path),
                    "caption_preview": "A traceable resonant mechanism.",
                    "argumentative_purpose": (
                        "Explain the source-derived resonant mechanism."
                    ),
                    "placement_guidance": "after_paragraph_1",
                    "status": "traceable_source_pending_review",
                    "publication_eligible": False,
                    "figure_contract": {
                        "permission": {
                            "status": "requires_review",
                            "note": (
                                "Publication permission is not established."
                            ),
                        }
                    },
                }
            ],
            "conceptual_figure_requests": [
                {
                    "request_id": "CR-S01-01",
                    "section_id": "S01",
                    "figure_kind": "mechanism_schematic",
                    "argumentative_purpose": (
                        "Generate a mechanism overview if approved later."
                    ),
                    "status": "pending_generation_and_review",
                }
            ],
            "unfilled_visual_needs": [],
        },
        "generation_policy": {
            "generation_requests_emitted": True,
            "images_generated_by_bridge": False,
        },
    }
    plan_path = tmp_path / "STAGED_VISUAL_REMOUNT_FACTORY_PLAN.json"
    _write_json(plan_path, wrapped_plan)

    canonical, ingestion = normalize_visual_factory_plan(wrapped_plan)
    assert len(canonical["placements"]) == 1
    assert len(canonical["conceptual_figure_requests"]) == 1
    assert ingestion["source_container"] == "editorial_plan"
    assert ingestion["generation_requests_are_materialized_figures"] is False

    package = VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=tmp_path / "factory-remount",
            real_visual_audit=False,
            real_image_generation=False,
            test_mode=True,
            max_generated_images=0,
        )
    ).run(
        visual_plan_path=plan_path,
        blueprint=_blueprint(),
        review_work_dir=tmp_path / "review",
    )

    assert len(package["figures"]) == 1
    figure = package["figures"][0]
    assert figure["source_route"] == "source_derived"
    assert figure["publication_eligible"] is False
    assert figure["permission"]["status"] == "requires_review"
    assert package["visual_cost_report"]["generated_figures"] == 0
    assert package["article_visual_contract"]["slot_count"] == 2
    assert package["visual_plan_ingestion"] == {
        "input_schema_version": (
            "optomind.staged_visual_remount.v1."
            "factory_plan_reference.v1"
        ),
        "canonical_plan_schema_version": (
            "research_harness.visual_editorial_plan.v1"
        ),
        "source_container": "editorial_plan",
        "placement_count": 1,
        "generation_request_count": 1,
        "unfilled_need_count": 0,
        "generation_requests_are_materialized_figures": False,
        "retained_placement_count": 1,
        "retained_generation_request_count": 1,
    }
    assert any(
        row.get("request_id") == "CR-S01-01"
        for row in package["unfilled_visual_opportunities"]
    )
    assert not any(
        row.get("source_route") == "conceptual_generated"
        for row in package["figures"]
    )
    assert figure["panel_manifest"][0]["paper_id"] == "paper-remount-1"


def test_factory_composes_explicit_group_and_keeps_provenance(
    tmp_path: Path,
):
    first = _image(tmp_path / "first.png", "navy")
    second = _image(tmp_path / "second.png", "darkred")
    plan_path = tmp_path / "VISUAL_EDITORIAL_PLAN.json"
    placements = []
    for index, path in enumerate((first, second), 1):
        placements.append(
            {
                "section_id": "S01",
                "visual_chunk_id": f"vis-{index}",
                "paper_id": f"paper-{index}",
                "doi": f"10.1/{index}",
                "local_image_path": str(path),
                "caption_preview": f"Source panel {index}.",
                "argumentative_purpose": (
                    "Compare complementary resonant mechanism evidence."
                ),
                "placement_guidance": "S01",
                "composite_group_id": "FIG-COMP-001",
                "panel_role": f"role-{index}",
                "status": "verified_existing",
            }
        )
    _write_json(
        plan_path,
        {
            "placements": placements,
            "conceptual_figure_requests": [],
            "unfilled_visual_needs": [],
        },
    )
    package = VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=tmp_path / "factory",
            test_mode=True,
        )
    ).run(
        visual_plan_path=plan_path,
        blueprint=_blueprint(),
        review_work_dir=tmp_path / "review",
    )
    assert len(package["figures"]) == 1
    figure = package["figures"][0]
    assert figure["figure_type"] == "source_composite"
    assert Path(figure["local_path"]).is_file()
    assert {
        panel["paper_id"] for panel in figure["panel_manifest"]
    } == {"paper-1", "paper-2"}


def test_generated_visual_enters_final_package_via_injected_generator(
    tmp_path: Path,
):
    class FakeGenerator:
        def __init__(self, *, output_dir: Path, **_: object) -> None:
            self.output_dir = Path(output_dir)

        def generate(self, *, plan: dict, section: dict) -> dict:
            del section
            path = _image(
                self.output_dir / f"{plan['visual_plan_id']}.png",
                "teal",
            )
            provenance = path.with_suffix(".provenance.json")
            _write_json(provenance, {"model": "fake"})
            return {
                **plan,
                "generation_status": "model_approved_human_pending",
                "local_image_path": str(path),
                "provenance_path": str(provenance),
                "model_review": {"misleading_elements": []},
            }

    plan_path = tmp_path / "VISUAL_EDITORIAL_PLAN.json"
    _write_json(
        plan_path,
        {
            "placements": [],
            "conceptual_figure_requests": [
                {
                    "section_id": "S01",
                    "figure_kind": "mechanism_schematic",
                    "argumentative_purpose": (
                        "Explain the causal resonant sensing mechanism."
                    ),
                    "generation_brief": (
                        "Draw a reader-facing resonant sensing mechanism."
                    ),
                    "data_provenance_level": "schematic",
                    "status": "pending_generation_and_review",
                }
            ],
            "unfilled_visual_needs": [],
        },
    )
    package = VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=tmp_path / "factory",
            real_image_generation=True,
            test_mode=True,
            max_generated_images=1,
        ),
        conceptual_generator_factory=FakeGenerator,
    ).run(
        visual_plan_path=plan_path,
        blueprint=_blueprint(),
        review_work_dir=tmp_path / "review",
    )
    assert len(package["figures"]) == 1
    figure = package["figures"][0]
    assert figure["source_route"] == "conceptual_generated"
    assert "AI-generated" in figure["caption_en"]
    assert figure["review_decision"] == "system_approved_test_mode"


def test_request_only_package_is_degraded_not_success(tmp_path: Path):
    plan_path = tmp_path / "VISUAL_EDITORIAL_PLAN.json"
    _write_json(
        plan_path,
        {
            "placements": [],
            "conceptual_figure_requests": [
                {
                    "section_id": "S01",
                    "figure_kind": "mechanism_schematic",
                    "argumentative_purpose": (
                        "Explain the causal resonant sensing mechanism."
                    ),
                    "generation_brief": (
                        "Draw a reader-facing resonant sensing mechanism."
                    ),
                }
            ],
            "unfilled_visual_needs": [],
        },
    )
    output_dir = tmp_path / "factory"
    VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=output_dir,
            real_image_generation=False,
            test_mode=True,
        )
    ).run(
        visual_plan_path=plan_path,
        blueprint=_blueprint(),
        review_work_dir=tmp_path / "review",
    )
    assert validate_final_visual_package_file(
        output_dir / "FINAL_VISUAL_PACKAGE.json"
    ).startswith("VALIDATION_DEGRADED")


def test_latex_copy_accepts_final_visual_package(tmp_path: Path):
    source = _image(tmp_path / "source.png")
    final_package = {
        "figures": [
            {
                "figure_id": "FIG-001",
                "section_id": "S01",
                "local_path": str(source),
                "caption_en": "A source-derived optical mechanism.",
                "review_decision": "system_approved_test_mode",
                "render_status": "ready",
            }
        ]
    }
    copied, rejected = _copy_verified_figures(
        final_package,
        output_dir=tmp_path / "latex",
    )
    assert not rejected
    assert len(copied) == 1
    assert copied[0]["caption_preview"] == (
        "A source-derived optical mechanism."
    )
    assert (tmp_path / "latex" / copied[0]["latex_path"]).is_file()


def test_exact_data_visual_uses_zero_cost_deterministic_route(
    tmp_path: Path,
):
    plan_path = tmp_path / "VISUAL_EDITORIAL_PLAN.json"
    _write_json(
        plan_path,
        {
            "placements": [],
            "conceptual_figure_requests": [
                {
                    "visual_plan_id": "DATA-01",
                    "section_id": "S01",
                    "figure_kind": "data_infographic",
                    "argumentative_purpose": "Compare resonance response.",
                    "data_provenance_level": "exact",
                    "input_data": {
                        "series": [
                            {
                                "label": "Q factor",
                                "x": [1, 2, 3],
                                "y": [100, 230, 410],
                            }
                        ]
                    },
                }
            ],
            "unfilled_visual_needs": [],
        },
    )
    package = VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=tmp_path / "factory",
            real_image_generation=True,
            test_mode=True,
        )
    ).run(
        visual_plan_path=plan_path,
        blueprint=_blueprint(),
        review_work_dir=tmp_path / "review",
    )
    figure = package["figures"][0]
    assert figure["source_route"] == "explanatory_data_visual"
    assert Path(figure["local_path"]).is_file()
    assert package["visual_cost_report"]["image_generation_calls"] == 0




def test_structured_diagram_renderer_is_local_and_legible(
    tmp_path: Path,
):
    result = ConceptualDiagramRenderer(
        output_dir=tmp_path / "diagram",
        real_llm=False,
    ).render(
        plan={
            "figure_kind": "mechanism_schematic",
            "argumentative_purpose": "Explain an optical mechanism.",
        },
        section={"title": "Optical mechanism"},
        figure_id="structured-01",
    )
    assert result["status"] == "ready"
    assert Path(result["local_image_path"]).is_file()
    spec = result["spec"]
    assert len(spec["nodes"]) >= 3
    assert all(node["label"] for node in spec["nodes"])


def test_selected_source_audit_cache_reuses_across_run_directories(
    tmp_path: Path,
):
    image_path = _image(tmp_path / "source.png")
    plan_path = tmp_path / "VISUAL_EDITORIAL_PLAN.json"
    _write_json(
        plan_path,
        {
            "placements": [
                {
                    "section_id": "S01",
                    "visual_chunk_id": "vis-cache-1",
                    "paper_id": "paper-cache-1",
                    "doi": "10.1/cache",
                    "local_image_path": str(image_path),
                    "caption_preview": "A traceable resonance figure.",
                    "argumentative_purpose": (
                        "Explain the optical resonance mechanism."
                    ),
                    "placement_guidance": "S01",
                    "status": "traceable_source_pending_review",
                }
            ],
            "conceptual_figure_requests": [],
            "unfilled_visual_needs": [],
        },
    )
    calls = {"count": 0}

    def first_vision_call(*_: object, **__: object) -> dict:
        calls["count"] += 1
        return {
            "content": json.dumps(
                {
                    "verdict": "approve",
                    "usefulness": "high",
                    "misleading_risk": "low",
                }
            ),
            "_llm_usage": {
                "model_name": "qwen-vl-plus",
                "input_tokens": 100,
                "output_tokens": 30,
            },
        }

    shared_cache = tmp_path / "shared-cache"
    first = VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=tmp_path / "run-1",
            shared_cache_dir=shared_cache,
            real_visual_audit=True,
            test_mode=True,
        ),
        vision_call=first_vision_call,
    ).run(
        visual_plan_path=plan_path,
        blueprint=_blueprint(),
        review_work_dir=tmp_path / "review",
    )
    assert calls["count"] == 1
    assert first["visual_cost_report"]["vision_calls"] == 1

    def must_not_run(*_: object, **__: object) -> dict:
        raise AssertionError("cross-run cache was not reused")

    second = VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=tmp_path / "run-2",
            shared_cache_dir=shared_cache,
            real_visual_audit=True,
            test_mode=True,
        ),
        vision_call=must_not_run,
    ).run(
        visual_plan_path=plan_path,
        blueprint=_blueprint(),
        review_work_dir=tmp_path / "review",
    )
    assert second["visual_cost_report"]["cache_hits"] == 1
    assert second["visual_cost_report"]["vision_calls"] == 0
    assert second["visual_cost_report"]["estimated_cost_cny"] == 0.0


def test_factory_fingerprint_invalidates_changed_materialization_policy():
    common = {
        "visual_plan": {
            "placements": [],
            "conceptual_figure_requests": [],
        },
        "blueprint": _blueprint(),
        "real_visual_audit": True,
        "real_image_generation": True,
        "test_mode": False,
        "vision_model_tier": "vision_plus_model",
        "image_model": "qwen-image-2.0-pro",
    }
    first = build_visual_factory_input_fingerprint(
        **common,
        max_generated_images=2,
    )
    repeated = build_visual_factory_input_fingerprint(
        **common,
        max_generated_images=2,
    )
    changed = build_visual_factory_input_fingerprint(
        **common,
        max_generated_images=3,
    )
    assert first == repeated
    assert first != changed


def test_visual_cache_namespace_is_topic_scoped_and_in_fingerprints(tmp_path: Path):
    blueprint = _blueprint()
    namespace = derive_visual_cache_namespace(blueprint)
    other_blueprint = dict(blueprint)
    other_blueprint["review_thesis"] = "A different topic with a different mechanism."
    other_namespace = derive_visual_cache_namespace(other_blueprint)

    assert namespace != other_namespace
    assert scoped_visual_cache_dir(tmp_path, blueprint).name == namespace
    assert scoped_visual_cache_dir(tmp_path, other_blueprint).name == other_namespace

    cache_key_common = {
        "image_sha256": "abc",
        "section_context": {"section_id": "S01"},
        "purpose": "explain",
        "caption_preview": "caption",
        "prompt_sha256": "prompt-v1",
    }
    assert build_source_audit_cache_key(
        **cache_key_common,
        cache_namespace=namespace,
    ) != build_source_audit_cache_key(
        **cache_key_common,
        cache_namespace=other_namespace,
    )

    fingerprint_common = {
        "visual_plan": {
            "placements": [],
            "conceptual_figure_requests": [],
        },
        "blueprint": blueprint,
        "real_visual_audit": False,
        "real_image_generation": False,
        "test_mode": True,
        "vision_model_tier": "vision_plus_model",
        "image_model": "qwen-image-2.0-pro",
        "max_generated_images": 4,
    }
    assert build_visual_factory_input_fingerprint(
        **fingerprint_common,
        cache_namespace=namespace,
    ) != build_visual_factory_input_fingerprint(
        **fingerprint_common,
        cache_namespace=other_namespace,
    )


def test_conceptual_generation_uses_one_global_transport_budget(
    tmp_path: Path,
    monkeypatch,
):
    calls = {"count": 0}

    class _FailedResponse:
        status_code = 500
        code = "server_error"

    class _FakeMultiModalConversation:
        @staticmethod
        def call(**kwargs):
            calls["count"] += 1
            return _FailedResponse()

    monkeypatch.setitem(
        sys.modules,
        "dashscope",
        types.SimpleNamespace(
            MultiModalConversation=_FakeMultiModalConversation,
        ),
    )
    monkeypatch.setattr(
        "optomind_research.conceptual_visual_generator."
        "get_qwen_api_key_candidates",
        lambda: [
            {"api_key": "key-a", "api_key_source": "test-a"},
            {"api_key": "key-b", "api_key_source": "test-b"},
        ],
    )
    generator = ConceptualVisualGenerator(
        output_dir=tmp_path,
        model="qwen-image-3.0-pro",
        real_llm=True,
        max_transport_attempts=2,
        generation_timeout_seconds=15,
    )
    result = generator.generate(
        plan={
            "visual_plan_id": "GF01",
            "creation_class": "author_synthesized_conceptual_schematic",
            "figure_kind": "concept_map",
            "generation_brief": "Map the scientific argument.",
            "data_provenance_level": "schematic",
        },
        section={"section_id": "S01", "title": "Foundations"},
    )

    assert calls["count"] == 2
    assert len(result["generation_attempts"]) == 2
    assert result["generation_status"] == "generation_failed"


def test_visual_review_queue_preserves_truthful_timeout_state(
    tmp_path: Path,
):
    image = _image(tmp_path / "source.png")
    figures = [
        {
            "figure_id": "F1",
            "section_id": "S01",
            "local_path": str(image),
            "caption_en": "A useful source figure.",
            "source_route": "source_derived",
            "render_status": "ready",
        }
    ]
    queue = build_visual_review_queue(
        figures,
        test_mode=False,
        timeout_seconds=30,
    )
    accepted, rejected = apply_visual_review_queue(figures, queue)
    assert not rejected
    assert (
        accepted[0]["review_decision"]
        == "timeout_accepted_for_draft"
    )
    assert queue["waiting_seconds"] == 30


def test_visual_review_queue_human_rejection_is_fail_closed(
    tmp_path: Path,
):
    image = _image(tmp_path / "source.png")
    figures = [
        {
            "figure_id": "F1",
            "section_id": "S01",
            "local_path": str(image),
            "caption_en": "A useful source figure.",
            "source_route": "source_derived",
            "render_status": "ready",
        }
    ]
    queue = build_visual_review_queue(
        figures,
        test_mode=False,
        human_decisions={
            "F1": {"action": "reject", "note": "wrong"}
        },
    )
    accepted, rejected = apply_visual_review_queue(figures, queue)
    assert not accepted
    assert rejected[0]["render_status"] == "rejected"


def _pending_placement(
    image_path: Path,
    *,
    section_id: str = "S01",
    chunk_id: str = "vis-1",
    purpose: str = "Explain the optical resonance mechanism.",
    caption: str = "A traceable resonance figure.",
) -> dict:
    return {
        "section_id": section_id,
        "visual_chunk_id": chunk_id,
        "paper_id": "paper-1",
        "doi": "10.1/visual",
        "local_image_path": str(image_path),
        "caption_preview": caption,
        "source_caption": caption,
        "argumentative_purpose": purpose,
        "placement_guidance": section_id,
        "status": "traceable_source_pending_review",
        "permission": {
            "status": "requires_review",
            "license": "",
            "use_permission": "discovery_only",
        },
        "publication_eligible": False,
        "publication_eligible_reason": (
            "pending_or_permission_not_sufficient"
        ),
        "source_map": {"unit_id": "vis-1"},
        "source_attribution": {"paper_id": "paper-1"},
    }


def _vision_response(
    *,
    verdict: str = "approve",
    section_fit: str = "direct",
    misleading_risk: str = "low",
    usefulness: str = "usable explanatory source figure.",
    editorial_caption: str = "",
) -> dict:
    return {
        "content": json.dumps(
            {
                "verdict": verdict,
                "section_fit": section_fit,
                "usefulness": usefulness,
                "misleading_risk": misleading_risk,
                "editorial_caption": editorial_caption,
                "reason": "fixture verdict",
            }
        ),
        "_llm_usage": {
            "model_name": "qwen-vl-plus",
            "input_tokens": 100,
            "output_tokens": 30,
        },
    }


def _run_factory(
    tmp_path: Path,
    *,
    plan: dict,
    blueprint: dict,
    vision_call,
    shared_cache_dir: Path | None = None,
    output_dir: str = "out",
) -> dict:
    plan_path = tmp_path / "VISUAL_EDITORIAL_PLAN.json"
    _write_json(plan_path, plan)
    return VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=tmp_path / output_dir,
            shared_cache_dir=shared_cache_dir or (tmp_path / "cache"),
            real_visual_audit=True,
            test_mode=True,
        ),
        vision_call=vision_call,
    ).run(
        visual_plan_path=plan_path,
        blueprint=blueprint,
        review_work_dir=tmp_path / "review",
    )


def test_source_audit_receives_target_section_context_and_rejects_unrelated(
    tmp_path: Path,
):
    image_path = _image(tmp_path / "source.png")
    plan = {
        "placements": [
            _pending_placement(
                image_path,
                purpose="Explain the resonant sensing mechanism.",
            )
        ],
        "conceptual_figure_requests": [],
        "unfilled_visual_needs": [],
    }
    captured: dict = {}

    def vision_call(*args, **kwargs):
        prompt = str(args[1])
        payload_text = prompt.split("TASK JSON:\n", 1)[1]
        captured["payload"] = json.loads(payload_text)
        return _vision_response(
            verdict="approve",
            section_fit="unrelated",
            usefulness="not useful in this section.",
        )

    package = _run_factory(
        tmp_path,
        plan=plan,
        blueprint=_blueprint(),
        vision_call=vision_call,
    )
    context = captured["payload"]["section_context"]
    assert context["section_id"] == "S01"
    assert context["title"] == "Resonant mechanism"
    assert "Optical resonances connect mechanism" in context[
        "full_text_or_context_window"
    ]
    assert "Explain optical resonance" in context["argument_role"]
    assert context["intended_anchor"] == "S01"
    assert captured["payload"]["source_caption"]
    assert package["figures"] == []
    rejected = [
        row
        for row in package["unfilled_visual_opportunities"]
        if row.get("reason") == "selected_source_failed_shortlist_audit"
    ]
    assert len(rejected) == 1
    assert rejected[0]["audit"]["section_fit"] == "unrelated"


def test_source_audit_reject_with_medium_risk_is_respected(
    tmp_path: Path,
):
    image_path = _image(tmp_path / "source.png")
    plan = {
        "placements": [_pending_placement(image_path)],
        "conceptual_figure_requests": [],
        "unfilled_visual_needs": [],
    }
    package = _run_factory(
        tmp_path,
        plan=plan,
        blueprint=_blueprint(),
        vision_call=lambda *args, **kwargs: _vision_response(
            verdict="reject",
            section_fit="weak",
            misleading_risk="medium",
            usefulness="misleading for this section.",
        ),
    )
    assert package["figures"] == []
    assert any(
        row.get("reason") == "selected_source_failed_shortlist_audit"
        for row in package["unfilled_visual_opportunities"]
    )


def test_source_audit_weak_background_is_accepted(tmp_path: Path):
    image_path = _image(tmp_path / "source.png")
    plan = {
        "placements": [_pending_placement(image_path)],
        "conceptual_figure_requests": [],
        "unfilled_visual_needs": [],
    }
    package = _run_factory(
        tmp_path,
        plan=plan,
        blueprint=_blueprint(),
        vision_call=lambda *args, **kwargs: _vision_response(
            verdict="approve",
            section_fit="weak",
            usefulness=(
                "Useful weak background for the resonant mechanism section."
            ),
        ),
    )
    assert len(package["figures"]) == 1
    audit = package["figures"][0]["source_audit"]
    assert audit["section_fit"] == "weak"
    assert audit["verdict"] == "approve"
    assert (
        package["figures"][0]["rights_notice"]
        == (
            "Internal-study use only; publication requires explicit "
            "permission from the source rights holder."
        )
    )
    assert package["figures"][0]["publication_eligible"] is False
    assert package["figures"][0]["permission"]["status"] == "requires_review"


def test_source_audit_cache_invalidated_by_section_context(
    tmp_path: Path,
):
    image_path = _image(tmp_path / "source.png")
    plan = {
        "placements": [_pending_placement(image_path)],
        "conceptual_figure_requests": [],
        "unfilled_visual_needs": [],
    }
    plan_path = tmp_path / "VISUAL_EDITORIAL_PLAN.json"
    _write_json(plan_path, plan)
    shared_cache = tmp_path / "shared-cache"
    calls = {"count": 0}

    def vision_call(*args, **kwargs):
        calls["count"] += 1
        return _vision_response()

    blueprint_a = _blueprint()
    blueprint_b = {
        **_blueprint(),
        "sections": [
            {
                **_blueprint()["sections"][0],
                "full_text": (
                    "A completely different section context for cache "
                    "invalidation."
                ),
                "argument_role": "Different argument role.",
            }
        ],
    }

    def run_once(blueprint: dict, output: str) -> dict:
        return VisualEvidenceFactory(
            VisualEvidenceFactoryConfig(
                output_dir=tmp_path / output,
                shared_cache_dir=shared_cache,
                real_visual_audit=True,
                test_mode=True,
            ),
            vision_call=vision_call,
        ).run(
            visual_plan_path=plan_path,
            blueprint=blueprint,
            review_work_dir=tmp_path / "review",
        )

    first = run_once(blueprint_a, "out-a")
    assert first["visual_cost_report"]["vision_calls"] == 1
    second = run_once(blueprint_b, "out-b")
    assert second["visual_cost_report"]["vision_calls"] == 1
    assert second["visual_cost_report"]["cache_hits"] == 0
    third = run_once(blueprint_b, "out-c")
    assert third["visual_cost_report"]["cache_hits"] == 1
    assert third["visual_cost_report"]["vision_calls"] == 0
    assert calls["count"] == 2


def test_source_audit_cache_key_includes_prompt_content():
    common = {
        "image_sha256": "abc",
        "section_context": {"section_id": "S01"},
        "purpose": "explain",
        "caption_preview": "caption",
    }
    first = build_source_audit_cache_key(
        **common,
        prompt_sha256="prompt-v1",
    )
    second = build_source_audit_cache_key(
        **common,
        prompt_sha256="prompt-v2",
    )
    assert first != second


def test_source_audit_transport_failure_is_fail_open_with_warnings(
    tmp_path: Path,
):
    image_path = _image(tmp_path / "source.png")
    plan = {
        "placements": [_pending_placement(image_path)],
        "conceptual_figure_requests": [],
        "unfilled_visual_needs": [],
    }

    def broken_vision(*args, **kwargs):
        raise RuntimeError("transport down")

    package = _run_factory(
        tmp_path,
        plan=plan,
        blueprint=_blueprint(),
        vision_call=broken_vision,
    )
    assert len(package["figures"]) == 1
    audit = package["figures"][0]["source_audit"]
    assert audit["audit_mode"] == "deterministic_traceability_fallback"
    assert audit["warnings"]
    assert package["validation"]["status"] == "passed"


def test_structural_policy_excludes_abstract_conclusion_and_caps_introduction(
    tmp_path: Path,
):
    abstract_image = _image(tmp_path / "abstract.png")
    intro_1 = _image(tmp_path / "intro-1.png")
    intro_2 = _image(tmp_path / "intro-2.png")
    s01_image = _image(tmp_path / "s01.png")
    s02_image = _image(tmp_path / "s02.png")
    conclusion_image = _image(tmp_path / "conclusion.png")
    plan = {
        "placements": [
            _pending_placement(
                abstract_image,
                section_id="Abstract",
                chunk_id="vis-abstract",
                purpose="Summarize the review landscape.",
            ),
            _pending_placement(
                intro_1,
                section_id="Introduction",
                chunk_id="vis-intro-1",
                purpose="Introduce the comparison axes.",
            ),
            _pending_placement(
                intro_2,
                section_id="Introduction",
                chunk_id="vis-intro-2",
                purpose="Introduce the path from simulation to experiment.",
            ),
            _pending_placement(
                s01_image,
                section_id="S01",
                chunk_id="vis-s01",
                purpose="Explain the resonant mechanism.",
            ),
            _pending_placement(
                s02_image,
                section_id="S02",
                chunk_id="vis-s02",
                purpose="Forward modeling accuracy.",
            ),
            _pending_placement(
                conclusion_image,
                section_id="Conclusion",
                chunk_id="vis-conclusion",
                purpose="Conclude with robust design.",
            ),
        ],
        "conceptual_figure_requests": [
            {
                "section_id": "Abstract",
                "figure_kind": "concept_map",
                "argumentative_purpose": "Abstract concept map.",
                "request_id": "CR-Abstract-01",
            },
            {
                "section_id": "Introduction",
                "figure_kind": "workflow_schematic",
                "argumentative_purpose": "Introduction workflow.",
                "request_id": "CR-Introduction-01",
            },
            {
                "section_id": "S01",
                "figure_kind": "mechanism_schematic",
                "argumentative_purpose": "S01 mechanism.",
                "request_id": "CR-S01-01",
            },
        ],
        "unfilled_visual_needs": [],
    }
    blueprint = {
        "review_thesis": "Mechanism to application.",
        "sections": [
            {
                "section_id": section_id,
                "title": section_id,
                "full_text": f"Full text of {section_id}.",
                "argument_role": f"Role of {section_id}.",
            }
            for section_id in (
                "Abstract",
                "Introduction",
                "S01",
                "S02",
                "Conclusion",
            )
        ],
    }
    _write_json(tmp_path / "VISUAL_EDITORIAL_PLAN.json", plan)
    package = VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=tmp_path / "out",
            real_visual_audit=False,
            test_mode=True,
            max_generated_images=0,
        )
    ).run(
        visual_plan_path=tmp_path / "VISUAL_EDITORIAL_PLAN.json",
        blueprint=blueprint,
        review_work_dir=tmp_path / "review",
    )
    figure_sections = {
        str(row.get("section_id") or "") for row in package["figures"]
    }
    assert figure_sections == {"Introduction", "S01", "S02"}
    assert "Abstract" not in figure_sections
    assert "Conclusion" not in figure_sections
    reasons = {
        str(row.get("reason") or "")
        for row in package["unfilled_visual_opportunities"]
    }
    assert "excluded_structural_policy_zero_visuals" in reasons
    assert "excluded_structural_policy_introduction_cap" in reasons
    # Introduction keeps exactly one total visual (one placement, no request).
    intro_figures = [
        row
        for row in package["figures"]
        if str(row.get("section_id") or "") == "Introduction"
    ]
    assert len(intro_figures) == 1


def test_apply_structural_visual_policy_is_deterministic():
    plan = {
        "placements": [
            {"section_id": "Abstract", "visual_chunk_id": "a"},
            {"section_id": "Introduction", "visual_chunk_id": "i1"},
            {"section_id": "Introduction", "visual_chunk_id": "i2"},
            {"section_id": "S01", "visual_chunk_id": "s1"},
            {"section_id": "Conclusion", "visual_chunk_id": "c"},
        ],
        "conceptual_figure_requests": [],
        "unfilled_visual_needs": [],
        "sections": [
            {
                "section_id": section_id,
                "title": section_id,
                "placements": [],
                "generation_requests": [],
                "placement_count": 0,
                "request_count": 0,
                "item_count": 0,
                "unfilled_needs": [],
            }
            for section_id in (
                "Abstract",
                "Introduction",
                "S01",
                "Conclusion",
            )
        ],
    }
    first = apply_structural_visual_policy(plan)
    second = apply_structural_visual_policy(plan)
    assert first == second
    assert [p["visual_chunk_id"] for p in first["placements"]] == [
        "i1",
        "s1",
    ]


class _RecordingGenerator:
    def __init__(self, **kwargs):
        self.calls: list[str] = []

    def generate(self, *, plan: dict, section: dict) -> dict:
        self.calls.append(str(plan.get("visual_plan_id") or ""))
        return {
            "local_image_path": str(
                section.get("_fixture_image_path") or ""
            ),
            "generation_status": "model_approved_human_pending",
            "model_review": {},
            "model_usage": {},
        }


def test_generated_selection_prefers_mechanism_then_workflow(
    tmp_path: Path,
):
    output_image = _image(tmp_path / "generated.png")
    plan = {
        "placements": [],
        "conceptual_figure_requests": [
            {
                "section_id": "S02",
                "figure_kind": "workflow_schematic",
                "argumentative_purpose": "Workflow first in file order.",
                "visual_plan_id": "W",
                "request_id": "CR-S02-01",
            },
            {
                "section_id": "S01",
                "figure_kind": "mechanism_schematic",
                "argumentative_purpose": "Mechanism second in file order.",
                "visual_plan_id": "M",
                "request_id": "CR-S01-01",
            },
            {
                "section_id": "S03",
                "figure_kind": "taxonomy_diagram",
                "argumentative_purpose": "Taxonomy third in file order.",
                "visual_plan_id": "T",
                "request_id": "CR-S03-01",
            },
        ],
        "unfilled_visual_needs": [],
    }
    blueprint = {
        "review_thesis": "Generic mechanism-to-application review.",
        "sections": [
            {
                "section_id": section_id,
                "title": section_id,
                "full_text": f"Full text of {section_id}.",
                "argument_role": f"Role of {section_id}.",
                "_fixture_image_path": str(output_image),
            }
            for section_id in ("S01", "S02", "S03")
        ],
    }
    plan_path = tmp_path / "VISUAL_EDITORIAL_PLAN.json"
    _write_json(plan_path, plan)
    generator = _RecordingGenerator()

    def generator_factory(**kwargs):
        return generator

    package = VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=tmp_path / "out",
            real_image_generation=False,
            test_mode=True,
            max_generated_images=2,
        ),
        conceptual_generator_factory=generator_factory,
    ).run(
        visual_plan_path=plan_path,
        blueprint=blueprint,
        review_work_dir=tmp_path / "review",
    )
    assert generator.calls == ["M", "W"]
    generated_ids = {
        str(row.get("figure_id") or "") for row in package["figures"]
    }
    assert generated_ids == {"M", "W"}
    unresolved_ids = {
        str(row.get("visual_plan_id") or "")
        for row in package["unfilled_visual_opportunities"]
    }
    assert "T" in unresolved_ids
    assert package["visual_cost_report"]["generated_figures"] == 2


def test_editorial_caption_from_vision_is_rendered_as_caption_en(
    tmp_path: Path,
):
    image_path = _image(tmp_path / "source.png")
    source_caption = (
        "Original long source figure caption with figure-level details."
    )
    plan = {
        "placements": [
            _pending_placement(image_path, caption=source_caption)
        ],
        "conceptual_figure_requests": [],
        "unfilled_visual_needs": [],
    }
    editorial = (
        "Optical resonance confinement schematic used here to explain "
        "the sensing mechanism."
    )
    package = _run_factory(
        tmp_path,
        plan=plan,
        blueprint=_blueprint(),
        vision_call=lambda *args, **kwargs: _vision_response(
            editorial_caption=editorial,
        ),
    )
    figure = package["figures"][0]
    assert figure["caption_en"] == editorial
    assert figure["source_caption"] == source_caption
    assert source_caption not in figure["caption_en"]
    assert figure["source_audit"]["editorial_caption"] == editorial


def test_editorial_caption_fallback_is_short_and_omits_source_caption(
    tmp_path: Path,
):
    image_path = _image(tmp_path / "source.png")
    source_caption = (
        "Original long source caption that must never leak into caption_en."
    )
    purpose = "Explain the resonant sensing mechanism."
    plan = {
        "placements": [
            _pending_placement(
                image_path,
                purpose=purpose,
                caption=source_caption,
            )
        ],
        "conceptual_figure_requests": [],
        "unfilled_visual_needs": [],
    }
    package = _run_factory(
        tmp_path,
        plan=plan,
        blueprint=_blueprint(),
        vision_call=lambda *args, **kwargs: _vision_response(),
    )
    figure = package["figures"][0]
    assert figure["caption_en"] == f"{purpose} Source: paper-1."
    assert source_caption not in figure["caption_en"]
    assert figure["source_caption"] == source_caption
    assert len(figure["caption_en"]) < 200


def test_editorial_caption_fallback_truncates_long_purpose(
    tmp_path: Path,
):
    image_path = _image(tmp_path / "source.png")
    source_caption = (
        "Long original source caption that must stay separated from the "
        "rendered editorial caption."
    )
    long_purpose = (
        "Explain the resonant sensing mechanism with detailed contextual "
        "background across fabrication, coupling, and measurement "
        "conditions that span multiple paragraphs of planner guidance. "
        "This purpose text is deliberately very long so the deterministic "
        "fallback must truncate it cleanly at a word boundary while keeping "
        "the source identity suffix intact. " * 3
    )
    plan = {
        "placements": [
            _pending_placement(
                image_path,
                purpose=long_purpose,
                caption=source_caption,
            )
        ],
        "conceptual_figure_requests": [],
        "unfilled_visual_needs": [],
    }
    package = _run_factory(
        tmp_path,
        plan=plan,
        blueprint=_blueprint(),
        vision_call=lambda *args, **kwargs: _vision_response(),
    )
    figure = package["figures"][0]
    caption = figure["caption_en"]
    assert len(caption) <= 280
    assert caption.endswith(" Source: paper-1.")
    assert source_caption not in caption
    assert figure["source_caption"] == source_caption
    assert "  " not in caption
    body = caption[: -len(" Source: paper-1.")]
    assert body
    assert not body.endswith(" ")


def test_editorial_caption_invalid_value_falls_back(tmp_path: Path):
    image_path = _image(tmp_path / "source.png")
    source_caption = "Traceable source figure caption."
    plan = {
        "placements": [
            _pending_placement(image_path, caption=source_caption)
        ],
        "conceptual_figure_requests": [],
        "unfilled_visual_needs": [],
    }

    def numeric_caption(*args, **kwargs):
        return _vision_response(editorial_caption=12345)

    package = _run_factory(
        tmp_path,
        plan=plan,
        blueprint=_blueprint(),
        vision_call=numeric_caption,
    )
    figure = package["figures"][0]
    assert figure["caption_en"] == (
        "Explain the optical resonance mechanism. Source: paper-1."
    )
    assert figure["source_caption"] == source_caption
    assert "12345" not in figure["caption_en"]


def test_editorial_caption_survives_audit_cache(tmp_path: Path):
    image_path = _image(tmp_path / "source.png")
    plan = {
        "placements": [_pending_placement(image_path)],
        "conceptual_figure_requests": [],
        "unfilled_visual_needs": [],
    }
    plan_path = tmp_path / "VISUAL_EDITORIAL_PLAN.json"
    _write_json(plan_path, plan)
    shared_cache = tmp_path / "shared-cache"
    editorial = "Cached editorial caption for the resonant mechanism section."
    calls = {"count": 0}

    def first_vision_call(*args, **kwargs):
        calls["count"] += 1
        return _vision_response(editorial_caption=editorial)

    def run_once(
        vision_call,
        output: str,
    ) -> dict:
        return VisualEvidenceFactory(
            VisualEvidenceFactoryConfig(
                output_dir=tmp_path / output,
                shared_cache_dir=shared_cache,
                real_visual_audit=True,
                test_mode=True,
            ),
            vision_call=vision_call,
        ).run(
            visual_plan_path=plan_path,
            blueprint=_blueprint(),
            review_work_dir=tmp_path / "review",
        )

    first = run_once(first_vision_call, "out-a")
    assert first["figures"][0]["caption_en"] == editorial
    assert calls["count"] == 1

    def must_not_run(*args, **kwargs):
        raise AssertionError("cached audit was not reused")

    second = run_once(must_not_run, "out-b")
    assert second["visual_cost_report"]["cache_hits"] == 1
    assert second["figures"][0]["caption_en"] == editorial
    assert calls["count"] == 1


def test_final_package_owns_source_figure_from_prior_run(
    tmp_path: Path,
):
    prior_image = _image(
        tmp_path / "prior-v2" / "visual_candidates" / "source.png"
    )
    plan = {
        "placements": [
            _pending_placement(
                prior_image,
                chunk_id="vis-prior",
                purpose="Explain the resonant sensing mechanism.",
            )
        ],
        "conceptual_figure_requests": [],
        "unfilled_visual_needs": [],
    }
    plan_path = tmp_path / "VISUAL_EDITORIAL_PLAN.json"
    _write_json(plan_path, plan)
    output_dir = tmp_path / "out"
    package = VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=output_dir,
            real_visual_audit=False,
            test_mode=True,
        )
    ).run(
        visual_plan_path=plan_path,
        blueprint=_blueprint(),
        review_work_dir=tmp_path / "review",
    )
    figure = package["figures"][0]
    owned_path = Path(figure["local_path"])
    assert owned_path.is_file()
    assert owned_path.parent == output_dir / "final_assets"
    assert figure["owned_asset_path"] == str(owned_path)
    assert figure["assets_owned_by_run"] is True
    assert figure["original_source_path"] == str(prior_image)
    # The acquisition source is never deleted and stays the panel provenance.
    assert prior_image.is_file()
    assert (
        figure["panel_manifest"][0]["source_local_path"]
        == str(prior_image)
    )
    assert package["validation"]["status"] == "passed"
    audit = json.loads(
        (
            output_dir / "FINAL_VISUAL_AUDIT_REPORT.json"
        ).read_text(encoding="utf-8")
    )
    assert audit["owned_asset_count"] == 1
    assert audit["owned_asset_directory"] == str(
        output_dir / "final_assets"
    )


def test_final_package_owns_generated_figure(tmp_path: Path):
    generated_image = _image(tmp_path / "generated-cache" / "gen.png")
    plan = {
        "placements": [],
        "conceptual_figure_requests": [
            {
                "section_id": "S01",
                "figure_kind": "mechanism_schematic",
                "argumentative_purpose": "Mechanism schematic.",
                "visual_plan_id": "GEN-OWNED",
                "request_id": "CR-S01-01",
            }
        ],
        "unfilled_visual_needs": [],
    }
    plan_path = tmp_path / "VISUAL_EDITORIAL_PLAN.json"
    _write_json(plan_path, plan)
    blueprint = {
        "review_thesis": "Mechanism to application.",
        "sections": [
            {
                "section_id": "S01",
                "title": "S01",
                "full_text": "Full text of S01.",
                "argument_role": "Role of S01.",
                "_fixture_image_path": str(generated_image),
            }
        ],
    }
    generator = _RecordingGenerator()

    def generator_factory(**kwargs):
        return generator

    output_dir = tmp_path / "out"
    package = VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=output_dir,
            real_image_generation=False,
            test_mode=True,
            max_generated_images=1,
        ),
        conceptual_generator_factory=generator_factory,
    ).run(
        visual_plan_path=plan_path,
        blueprint=blueprint,
        review_work_dir=tmp_path / "review",
    )
    figure = package["figures"][0]
    owned_path = Path(figure["local_path"])
    assert owned_path.is_file()
    assert owned_path.parent == output_dir / "final_assets"
    assert figure["assets_owned_by_run"] is True
    assert figure["original_source_path"] == str(generated_image)
    assert generated_image.is_file()
    assert package["validation"]["status"] == "passed"


def test_missing_source_is_omitted_from_owned_figures(tmp_path: Path):
    factory = VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=tmp_path / "out",
            real_visual_audit=False,
            test_mode=True,
        )
    )
    missing = tmp_path / "prior-run" / "missing.png"
    figures, report = factory._own_accepted_figures(
        [{"figure_id": "FIG-MISSING", "section_id": "S03", "local_path": str(missing)}]
    )

    assert figures == []
    assert report["owned"] == 0
    assert report["failures"] == [
        {
            "figure_id": "FIG-MISSING",
            "section_id": "S03",
            "reason": "source_file_missing",
            "original_source_path": str(missing),
        }
    ]


def test_copy_failure_is_omitted_and_audited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import optomind_research.runtime.visual_evidence_factory as module

    source = _image(tmp_path / "prior-run" / "source.png")
    factory = VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=tmp_path / "out",
            real_visual_audit=False,
            test_mode=True,
        )
    )

    def fail_copy(*_args, **_kwargs):
        raise OSError("simulated copy failure")

    monkeypatch.setattr(module.shutil, "copyfile", fail_copy)
    figures, report = factory._own_accepted_figures(
        [{"figure_id": "FIG-FAIL", "section_id": "S04", "local_path": str(source)}]
    )

    assert figures == []
    assert report["owned"] == 0
    assert len(report["failures"]) == 1
    failure = report["failures"][0]
    assert failure["figure_id"] == "FIG-FAIL"
    assert failure["section_id"] == "S04"
    assert failure["reason"] == "copy_failed"
    assert failure["original_source_path"] == str(source)
    assert "simulated copy failure" in failure["error"]
