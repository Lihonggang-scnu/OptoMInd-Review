"""Focused tests for the structured scientific diagram route.

The factory is exercised through ``_generated_figures`` with injected fake
renderer, raster generator, and vision callables, so no model/network API is
touched.  Test images are tiny placeholder files under the existing
``build/`` directory.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from optomind_research.runtime import visual_evidence_factory as vef_module
from optomind_research.runtime.visual_evidence_factory import (
    _resolve_generation_route,
    VisualEvidenceFactory,
    VisualEvidenceFactoryConfig,
)


def _build_root() -> Path:
    root = Path.cwd() / "build"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _artifact(prefix: str) -> Path:
    return _build_root() / (
        f"vt-structured-{prefix}-{uuid.uuid4().hex[:10]}.png"
    )


def _request(
    plan_id: str,
    *,
    figure_kind: str = "mechanism_schematic",
    visual_route: str = "",
) -> dict[str, Any]:
    request = {
        "visual_plan_id": plan_id,
        "section_id": "S01",
        "figure_kind": figure_kind,
        "argumentative_purpose": "Explain the causal mechanism.",
        "generation_brief": "Draw a reader-facing explanatory diagram.",
        "data_provenance_level": "schematic",
        "status": "pending_generation_and_review",
    }
    if visual_route:
        request["visual_route"] = visual_route
    return request


def _structured_ready(
    revision: int = 0,
    spec_origin: str = "generated",
) -> dict[str, Any]:
    path = _artifact("diagram")
    path.write_bytes(b"structured-diagram")
    return {
        "status": "ready",
        "local_image_path": str(path),
        "provenance_path": str(path.with_suffix(".provenance.json")),
        "spec": {
            "title": "Mechanism to application",
            "layout": "left_to_right",
            "nodes": [
                {"id": "N1", "label": "Input", "kind": "input"},
                {"id": "N2", "label": "Mechanism", "kind": "mechanism"},
                {"id": "N3", "label": "Outcome", "kind": "outcome"},
            ],
            "edges": [
                {"source": "N1", "target": "N2", "label": "drives"},
                {"source": "N2", "target": "N3", "label": "enables"},
            ],
            "takeaway": "Mechanism drives outcome.",
        },
        "model_usage": {"input_tokens": 120, "output_tokens": 90},
        "spec_origin": spec_origin,
        "revision": revision,
    }


class ScriptedStructuredRenderer:
    """Fake renderer implementing the public render/revise_spec APIs."""

    def __init__(
        self,
        *,
        output_dir: Path,
        real_llm: bool = True,
        model_tier: str = "standard_model",
        **_: object,
    ) -> None:
        del output_dir, real_llm, model_tier
        self.calls: list[dict[str, Any]] = []
        self.results: list[dict[str, Any]] = []

    def add(self, result: dict[str, Any]) -> "ScriptedStructuredRenderer":
        self.results.append(result)
        return self

    def render(
        self,
        *,
        plan: dict,
        section: dict,
        figure_id: str,
    ) -> dict[str, Any]:
        del section
        self.calls.append(
            {"action": "render", "plan": dict(plan), "figure_id": figure_id}
        )
        return self._next()

    def revise_spec(
        self,
        *,
        previous_spec: dict,
        reviewer_feedback: str,
        plan: dict,
        section: dict,
        figure_id: str,
        revision: int = 1,
    ) -> dict[str, Any]:
        del previous_spec, section
        self.calls.append(
            {
                "action": "revise_spec",
                "plan": dict(plan),
                "figure_id": figure_id,
                "reviewer_feedback": reviewer_feedback,
                "revision": revision,
            }
        )
        return self._next()

    def _next(self) -> dict[str, Any]:
        index = min(len(self.calls) - 1, len(self.results) - 1)
        return dict(self.results[index])


class RasterSpyGenerator:
    """Fake raster generator; records every call."""

    def __init__(self, *, output_dir: Path, **_: object) -> None:
        del output_dir
        self.calls: list[dict[str, Any]] = []

    def generate(self, *, plan: dict, section: dict) -> dict[str, Any]:
        del section
        self.calls.append(dict(plan))
        path = _artifact("raster")
        path.write_bytes(b"raster-image")
        return {
            **plan,
            "generation_status": "model_approved_human_pending",
            "local_image_path": str(path),
            "provenance_path": str(path.with_suffix(".provenance.json")),
            "model_review": {
                "verdict": "approve",
                "misleading_elements": [],
            },
        }


class RasterMockGenerator:
    """Fake raster generator for mock mode."""

    def __init__(self, *, output_dir: Path, **_: object) -> None:
        del output_dir
        self.calls: list[dict[str, Any]] = []

    def generate(self, *, plan: dict, section: dict) -> dict[str, Any]:
        del section
        self.calls.append(dict(plan))
        return {
            **plan,
            "generation_status": "mock_not_generated",
            "model_review": {},
        }


class DefaultFakeRenderer:
    """Fake default renderer class used via module-level monkeypatch."""

    instances: list["DefaultFakeRenderer"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        DefaultFakeRenderer.instances.append(self)
        self.calls: list[dict[str, Any]] = []

    def render(
        self,
        *,
        plan: dict,
        section: dict,
        figure_id: str,
    ) -> dict[str, Any]:
        del plan, section
        self.calls.append({"action": "render", "figure_id": figure_id})
        return _structured_ready()

    def revise_spec(
        self,
        *,
        previous_spec: dict,
        reviewer_feedback: str,
        plan: dict,
        section: dict,
        figure_id: str,
        revision: int = 1,
    ) -> dict[str, Any]:
        del previous_spec, reviewer_feedback, plan, section
        self.calls.append({"action": "revise_spec", "figure_id": figure_id})
        return _structured_ready(revision=revision, spec_origin="revision")


class BoomRenderer:
    """Raises if the structured route is used behind a custom generator seam."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError(
            "structured route must not run for a custom generator seam"
        )


def _vision(decisions: list[str]) -> Any:
    state = {"calls": 0}

    def vision_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        verdict = decisions[min(state["calls"], len(decisions) - 1)]
        state["calls"] += 1
        content: dict[str, Any] = {"verdict": verdict}
        if verdict in {"revise", "reject"}:
            content["required_revisions"] = [f"feedback-{state['calls']}"]
            content["misleading_elements"] = ["Legend is unclear"]
        return {
            "content": json.dumps(content),
            "_llm_usage": {"input_tokens": 60, "output_tokens": 40},
            "_vision_used": True,
        }

    return vision_call


def _factory(
    *,
    renderer: ScriptedStructuredRenderer,
    raster: RasterSpyGenerator | RasterMockGenerator,
    vision: Any,
    **overrides: Any,
) -> VisualEvidenceFactory:
    config_kwargs = {
        "output_dir": _build_root(),
        "real_image_generation": True,
        "test_mode": True,
        "max_generated_images": 2,
        **overrides,
    }
    return VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(**config_kwargs),
        vision_call=vision,
        conceptual_generator_factory=lambda **_: raster,
        diagram_renderer_factory=lambda **_: renderer,
    )


def test_structured_route_approve_first_attempt() -> None:
    root = _build_root()
    renderer = ScriptedStructuredRenderer(
        output_dir=root
    ).add(_structured_ready())
    raster = RasterSpyGenerator(output_dir=root)
    factory = _factory(
        renderer=renderer,
        raster=raster,
        vision=_vision(["approve"]),
    )
    generated, unresolved = factory._generated_figures(
        [_request("V-ST-1", figure_kind="mechanism_schematic")],
        blueprint={"sections": []},
    )
    try:
        assert len(generated) == 1
        assert unresolved == []
        assert len(renderer.calls) == 1
        assert renderer.calls[0]["action"] == "render"
        assert raster.calls == []
        figure = generated[0]
        assert figure["figure_type"] == "structured_explanatory_diagram"
        assert "AI-assisted explanatory diagram" in figure["caption_en"]
        assert "not empirical" in figure["caption_en"]
        assert figure["panel_manifest"][0]["generation_model"] == (
            "qwen_text_spec_plus_graphviz"
        )
        result = figure["generation_result"]
        assert result["generation_total_attempts"] == 1
        assert len(result["structured_attempts"]) == 1
        assert result["structured_spec"]["title"] == (
            "Mechanism to application"
        )
        assert factory.cost["image_generation_calls"] == 0
        assert factory.cost["image_generation_reference_cost_cny"] == 0.0
        assert factory.cost["diagram_spec_calls"] == 1
        assert factory.cost["diagram_spec_input_tokens"] == 120
        assert factory.cost["vision_calls"] == 1
        assert factory.cost["vision_input_tokens"] == 60
    finally:
        for row in generated:
            Path(str(row.get("local_path") or "")).unlink(missing_ok=True)


def test_structured_revise_then_approve() -> None:
    root = _build_root()
    renderer = ScriptedStructuredRenderer(output_dir=root)
    renderer.add(_structured_ready())
    renderer.add(_structured_ready(revision=1, spec_origin="revision"))
    raster = RasterSpyGenerator(output_dir=root)
    factory = _factory(
        renderer=renderer,
        raster=raster,
        vision=_vision(["revise", "approve"]),
    )
    generated, unresolved = factory._generated_figures(
        [_request("V-ST-2", figure_kind="workflow_schematic")],
        blueprint={"sections": []},
    )
    try:
        assert len(generated) == 1
        assert unresolved == []
        assert [call["action"] for call in renderer.calls] == [
            "render",
            "revise_spec",
        ]
        assert renderer.calls[1]["reviewer_feedback"] == (
            "feedback-1 | Legend is unclear | Review verdict: revise"
        )
        assert renderer.calls[1]["revision"] == 1
        assert raster.calls == []
        result = generated[0]["generation_result"]
        assert result["generation_total_attempts"] == 2
        assert len(result["structured_attempts"]) == 2
        assert (
            result["structured_attempts"][0]["review"]["verdict"]
            == "revise"
        )
        assert (
            result["structured_attempts"][1]["review"]["verdict"]
            == "approve"
        )
        assert result["structured_attempts"][1]["reviewer_feedback"] == (
            "feedback-1 | Legend is unclear | Review verdict: revise"
        )
        assert factory.cost["diagram_spec_calls"] == 2
        assert factory.cost["vision_calls"] == 2
    finally:
        for row in generated:
            Path(str(row.get("local_path") or "")).unlink(missing_ok=True)


def test_structured_exhaust_three_attempts_nonblocking() -> None:
    root = _build_root()
    renderer = ScriptedStructuredRenderer(output_dir=root)
    for index in range(3):
        renderer.add(
            _structured_ready(
                revision=index,
                spec_origin="generated" if index == 0 else "revision",
            )
        )
    raster = RasterSpyGenerator(output_dir=root)
    factory = _factory(
        renderer=renderer,
        raster=raster,
        vision=_vision(["revise", "revise", "revise"]),
    )
    generated, unresolved = factory._generated_figures(
        [_request("V-ST-3", figure_kind="concept_map")],
        blueprint={"sections": []},
    )
    result: dict = {}
    try:
        # Private-study relaxation: the third structured attempt rendered, so
        # it is placed with disclosure rather than discarded.  Everything
        # about the retry ladder itself is unchanged -- three spec calls,
        # three vision calls, no raster fallback, no image generation.
        assert unresolved == []
        assert len(generated) == 1
        figure = generated[0]
        assert figure["salvaged_over_reviewer_objection"] is True
        result = figure["generation_result"]
        assert result["generation_attempts_exhausted"] is True
        assert result["generation_salvaged_last_attempt"] is True
        assert len(result["structured_attempts"]) == 3
        assert result["generation_retry_stop_reason"] == (
            "attempts_exhausted"
        )
        assert [call["action"] for call in renderer.calls] == [
            "render",
            "revise_spec",
            "revise_spec",
        ]
        assert raster.calls == []
        assert factory.cost["diagram_spec_calls"] == 3
        assert factory.cost["vision_calls"] == 3
        assert factory.cost["image_generation_calls"] == 0
    finally:
        for attempt in (
            result.get("structured_attempts") or []
        ):
            Path(str(attempt.get("local_image_path") or "")).unlink(
                missing_ok=True
            )


def test_task_budget_still_caps_structured_requests() -> None:
    root = _build_root()
    renderer = ScriptedStructuredRenderer(
        output_dir=root
    ).add(_structured_ready())
    raster = RasterSpyGenerator(output_dir=root)
    factory = _factory(
        renderer=renderer,
        raster=raster,
        vision=_vision(["approve"]),
        max_generated_images=1,
    )
    generated, unresolved = factory._generated_figures(
        [
            _request("V-BUDGET-1", figure_kind="taxonomy_diagram"),
            _request("V-BUDGET-2", figure_kind="taxonomy_diagram"),
        ],
        blueprint={"sections": []},
    )
    try:
        assert len(generated) == 1
        assert len(unresolved) == 1
        assert unresolved[0]["reason"] == (
            "generation_task_budget_or_lower_priority"
        )
        assert len(renderer.calls) == 1
        assert renderer.calls[0]["plan"]["visual_plan_id"] == "V-BUDGET-1"
        assert raster.calls == []
    finally:
        for row in generated:
            Path(str(row.get("local_path") or "")).unlink(missing_ok=True)


def test_explicit_raster_route_still_used() -> None:
    root = _build_root()
    renderer = ScriptedStructuredRenderer(output_dir=root)
    raster = RasterSpyGenerator(output_dir=root)
    factory = _factory(
        renderer=renderer,
        raster=raster,
        vision=_vision(["approve"]),
    )
    generated, unresolved = factory._generated_figures(
        [
            _request(
                "V-RASTER",
                figure_kind="mechanism_schematic",
                visual_route="raster_image_generation",
            )
        ],
        blueprint={"sections": []},
    )
    try:
        assert len(generated) == 1
        assert unresolved == []
        assert len(raster.calls) == 1
        assert renderer.calls == []
        assert generated[0]["figure_type"] == "mechanism_schematic"
        assert "AI-generated explanatory schematic" in (
            generated[0]["caption_en"]
        )
    finally:
        for row in generated:
            Path(str(row.get("local_path") or "")).unlink(missing_ok=True)


def test_non_text_heavy_kind_defaults_to_raster() -> None:
    root = _build_root()
    renderer = ScriptedStructuredRenderer(output_dir=root)
    raster = RasterSpyGenerator(output_dir=root)
    factory = _factory(
        renderer=renderer,
        raster=raster,
        vision=_vision(["approve"]),
    )
    generated, unresolved = factory._generated_figures(
        [_request("V-TREND", figure_kind="trend_schematic")],
        blueprint={"sections": []},
    )
    try:
        assert len(generated) == 1
        assert unresolved == []
        assert len(raster.calls) == 1
        assert renderer.calls == []
    finally:
        for row in generated:
            Path(str(row.get("local_path") or "")).unlink(missing_ok=True)


def test_mock_mode_text_heavy_keeps_raster_mock_path() -> None:
    root = _build_root()
    renderer = ScriptedStructuredRenderer(output_dir=root)
    raster = RasterMockGenerator(output_dir=root)
    factory = _factory(
        renderer=renderer,
        raster=raster,
        vision=_vision(["approve"]),
        real_image_generation=False,
    )
    generated, unresolved = factory._generated_figures(
        [_request("V-MOCK", figure_kind="mechanism_schematic")],
        blueprint={"sections": []},
    )
    assert generated == []
    assert len(raster.calls) == 1
    assert renderer.calls == []
    assert unresolved[0]["reason"] == "mock_not_generated"


def test_route_resolver_default_and_custom_generator_seam() -> None:
    request = _request("R-SEAM", figure_kind="mechanism_schematic")
    assert _resolve_generation_route(request) == "structured_diagram"
    assert (
        _resolve_generation_route(request, custom_generator_seam=True)
        == "raster_image_generation"
    )
    explicit_structured = _request(
        "R-EXPLICIT-ST",
        figure_kind="mechanism_schematic",
        visual_route="structured_diagram",
    )
    assert (
        _resolve_generation_route(
            explicit_structured,
            custom_generator_seam=True,
        )
        == "structured_diagram"
    )
    explicit_raster = _request(
        "R-EXPLICIT-RASTER",
        figure_kind="mechanism_schematic",
        visual_route="raster_image_generation",
    )
    assert (
        _resolve_generation_route(
            explicit_raster,
            custom_generator_seam=True,
        )
        == "raster_image_generation"
    )


def test_explicit_custom_generator_preserves_raster_seam(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        vef_module,
        "ConceptualDiagramRenderer",
        BoomRenderer,
    )
    root = _build_root()
    raster = RasterSpyGenerator(output_dir=root)
    factory = VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=root,
            real_image_generation=True,
            test_mode=True,
            max_generated_images=2,
        ),
        conceptual_generator_factory=lambda **_: raster,
    )
    generated, unresolved = factory._generated_figures(
        [_request("V-SEAM", figure_kind="mechanism_schematic")],
        blueprint={"sections": []},
    )
    try:
        assert factory._conceptual_generator_factory_explicit is True
        assert factory._diagram_renderer_factory_explicit is False
        assert len(raster.calls) == 1
        assert len(generated) == 1
        assert unresolved == []
        assert generated[0]["figure_type"] == "mechanism_schematic"
        assert "AI-generated explanatory schematic" in (
            generated[0]["caption_en"]
        )
    finally:
        for row in generated:
            Path(str(row.get("local_path") or "")).unlink(missing_ok=True)


def test_normal_default_construction_keeps_structured_route(
    monkeypatch: Any,
) -> None:
    DefaultFakeRenderer.instances = []
    monkeypatch.setattr(
        vef_module,
        "ConceptualDiagramRenderer",
        DefaultFakeRenderer,
    )
    factory = VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=_build_root(),
            real_image_generation=True,
            test_mode=True,
            max_generated_images=2,
        ),
        vision_call=_vision(["approve"]),
    )
    generated, unresolved = factory._generated_figures(
        [_request("V-DEFAULT", figure_kind="workflow_schematic")],
        blueprint={"sections": []},
    )
    try:
        assert factory._conceptual_generator_factory_explicit is False
        assert factory._diagram_renderer_factory_explicit is False
        assert len(DefaultFakeRenderer.instances) == 1
        renderer = DefaultFakeRenderer.instances[0]
        assert renderer.calls[0]["action"] == "render"
        assert len(generated) == 1
        assert unresolved == []
        assert (
            generated[0]["figure_type"]
            == "structured_explanatory_diagram"
        )
        assert "AI-assisted explanatory diagram" in (
            generated[0]["caption_en"]
        )
    finally:
        for row in generated:
            Path(str(row.get("local_path") or "")).unlink(missing_ok=True)
