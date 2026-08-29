"""Focused retry-loop tests for the conceptual generation-review loop.

The loop is exercised through ``VisualEvidenceFactory._generated_figures``
with a scripted generator, so no model, network, or image processing API is
touched.  Test images are tiny placeholder files written under the existing
``build/`` directory (Python-created temp directories are not writable in
this sandbox).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

from optomind_research.runtime.visual_evidence_factory import (
    MAX_GENERATION_TOTAL_ATTEMPTS,
    VisualEvidenceFactory,
    VisualEvidenceFactoryConfig,
    build_article_visual_contract,
)


def _build_root() -> Path:
    root = Path.cwd() / "build"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _artifact(prefix: str) -> Path:
    return _build_root() / (
        f"vt-factory-{prefix}-{uuid.uuid4().hex[:10]}.png"
    )


def _request(plan_id: str) -> dict[str, Any]:
    return {
        "visual_plan_id": plan_id,
        "section_id": "S01",
        "figure_kind": "mechanism_schematic",
        "argumentative_purpose": (
            "Explain the causal resonant sensing mechanism."
        ),
        "generation_brief": "Draw a reader-facing explanatory mechanism.",
        "data_provenance_level": "schematic",
        "status": "pending_generation_and_review",
        # These tests exercise the raster generation-review loop, so the
        # text-heavy kind must explicitly opt out of the default structured
        # diagram route.
        "visual_route": "raster_image_generation",
    }


def _approved(path: Path) -> dict[str, Any]:
    return {
        "generation_status": "model_approved_human_pending",
        "local_image_path": str(path),
        "provenance_path": str(path.with_suffix(".provenance.json")),
        "model_review": {
            "verdict": "approve",
            "misleading_elements": [],
        },
    }


def _approved_with_usage(
    path: Path,
    *,
    model_name: str,
    input_tokens: int,
    output_tokens: int,
) -> dict[str, Any]:
    return {
        **_approved(path),
        "model_review_usage": {
            "model_name": model_name,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


def _revision(path: Path, feedback: list[str]) -> dict[str, Any]:
    return {
        "generation_status": "model_rejected_or_revision_required",
        "local_image_path": str(path),
        "provenance_path": str(path.with_suffix(".provenance.json")),
        "model_review": {
            "verdict": "revise",
            "required_revisions": feedback,
            "misleading_elements": ["Legend is unclear"],
        },
    }


class ScriptedGenerator:
    """Fake conceptual generator returning pre-scripted results."""

    def __init__(self, *, output_dir: Path, **_: object) -> None:
        del output_dir
        self.calls: list[dict[str, Any]] = []
        self.results: list[dict[str, Any]] = []

    def add(self, result: dict[str, Any]) -> "ScriptedGenerator":
        self.results.append(result)
        return self

    def generate(self, *, plan: dict, section: dict) -> dict:
        del section
        self.calls.append(dict(plan))
        result = dict(
            self.results[
                min(len(self.calls) - 1, len(self.results) - 1)
            ]
        )
        local_path = result.get("local_image_path")
        if local_path:
            path = Path(str(local_path))
            if not path.is_file():
                path.write_bytes(b"fake-image")
        return {**plan, **result}


def _factory(
    generator: ScriptedGenerator,
    *,
    vision_call: Any = None,
    diagram_renderer_factory: Any = None,
    **overrides: Any,
) -> VisualEvidenceFactory:
    config_kwargs = {
        "output_dir": _build_root(),
        "real_image_generation": True,
        "test_mode": True,
        "max_generated_images": 1,
        **overrides,
    }
    constructor_kwargs: dict[str, Any] = {
        "conceptual_generator_factory": lambda **_: generator,
    }
    if vision_call is not None:
        constructor_kwargs["vision_call"] = vision_call
    if diagram_renderer_factory is not None:
        constructor_kwargs["diagram_renderer_factory"] = (
            diagram_renderer_factory
        )
    return VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(**config_kwargs),
        **constructor_kwargs,
    )


def test_approve_first_attempt_uses_single_attempt() -> None:
    root = _build_root()
    path = _artifact("approve")
    generator = ScriptedGenerator(output_dir=root).add(_approved(path))
    factory = _factory(generator)
    try:
        generated, unresolved = factory._generated_figures(
            [_request("V-APPROVE")],
            blueprint={"sections": []},
        )
        assert len(generator.calls) == 1
        assert len(generated) == 1
        assert unresolved == []
        result = generated[0]["generation_result"]
        assert result["generation_total_attempts"] == 1
        assert "generation_attempts_exhausted" not in result
        assert "retry_history" not in result
    finally:
        path.unlink(missing_ok=True)


def test_approve_after_revision_uses_reviewer_feedback() -> None:
    root = _build_root()
    first = _artifact("rev-a")
    second = _artifact("rev-b")
    generator = (
        ScriptedGenerator(output_dir=root)
        .add(_revision(first, ["Add clear axis labels"]))
        .add(_approved(second))
    )
    factory = _factory(generator)
    try:
        generated, unresolved = factory._generated_figures(
            [_request("V-REVISE")],
            blueprint={"sections": []},
        )
        assert len(generator.calls) == 2
        assert len(generated) == 1
        assert unresolved == []
        result = generated[0]["generation_result"]
        assert result["generation_total_attempts"] == 2
        assert len(result["retry_history"]) == 1
        brief = generator.calls[1]["generation_brief"]
        assert "Add clear axis labels" in brief
        assert "Legend is unclear" in brief
        assert "unreadable text" not in brief
        assert "never invent data" in brief
    finally:
        first.unlink(missing_ok=True)
        second.unlink(missing_ok=True)


def test_exhaust_three_attempts_records_nonblocking_unresolved_need() -> None:
    root = _build_root()
    paths = [_artifact("exhaust") for _ in range(3)]
    generator = ScriptedGenerator(output_dir=root)
    for index, path in enumerate(paths):
        generator.add(_revision(path, [f"Fix issue {index + 1}"]))
    factory = _factory(generator)
    try:
        generated, unresolved = factory._generated_figures(
            [_request("V-EXHAUST")],
            blueprint={"sections": []},
        )
        # Private-study relaxation: all three attempts rendered, so the last
        # one is placed with disclosure instead of being discarded.  The
        # retry machinery itself is unchanged -- still exactly three calls,
        # still feedback-carrying, still flagged exhausted on the result.
        assert unresolved == []
        assert len(generated) == 1
        assert len(generator.calls) == MAX_GENERATION_TOTAL_ATTEMPTS == 3
        figure = generated[0]
        assert figure["salvaged_over_reviewer_objection"] is True
        result = figure["generation_result"]
        assert result["generation_attempts_exhausted"] is True
        assert result["generation_retry_stop_reason"] == (
            "attempts_exhausted"
        )
        assert len(result["retry_history"]) == 2
        assert "Fix issue 2" in generator.calls[2]["generation_brief"]
        assert (
            result["model_review"]["required_revisions"][0]
            == "Fix issue 3"
        )
        contract = build_article_visual_contract(
            blueprint={"sections": []},
            visual_plan={
                "placements": [],
                "conceptual_figure_requests": [_request("V-EXHAUST")],
                "unfilled_visual_needs": [],
            },
        )
        assert contract["policy"]["missing_figure_invalidates_text"] is False
        assert (
            contract["policy"]["pending_requests_are_not_rendered_figures"]
            is True
        )
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


def test_failed_transport_status_retries_then_approves() -> None:
    root = _build_root()
    failed_path = _artifact("transport-a")
    approved_path = _artifact("transport-b")
    generator = (
        ScriptedGenerator(output_dir=root)
        .add(
            {
                "generation_status": "generation_failed",
                "generation_error": "transport_boom",
                "model_review": {},
            }
        )
        .add(_approved(approved_path))
    )
    factory = _factory(generator)
    try:
        generated, unresolved = factory._generated_figures(
            [_request("V-TRANSPORT")],
            blueprint={"sections": []},
        )
        assert len(generator.calls) == 2
        assert len(generated) == 1
        assert unresolved == []
        brief = generator.calls[1]["generation_brief"]
        assert "correcting the issues identified in review" in brief
        assert "unreadable text" not in brief
        assert (
            generated[0]["generation_result"]["generation_total_attempts"]
            == 2
        )
    finally:
        failed_path.unlink(missing_ok=True)
        approved_path.unlink(missing_ok=True)


def test_mock_mode_keeps_single_attempt_behavior() -> None:
    root = _build_root()
    generator = ScriptedGenerator(output_dir=root).add(
        {
            "generation_status": "mock_not_generated",
            "model_review": {},
        }
    )
    factory = _factory(generator, real_image_generation=False)
    generated, unresolved = factory._generated_figures(
        [_request("V-MOCK")],
        blueprint={"sections": []},
    )
    assert len(generator.calls) == 1
    assert generated == []
    assert unresolved[0]["reason"] == "mock_not_generated"


def test_default_max_total_attempts_is_three() -> None:
    config = VisualEvidenceFactoryConfig(output_dir=_build_root())
    assert (
        1 + config.max_generation_retries
        == MAX_GENERATION_TOTAL_ATTEMPTS
        == 3
    )
    clamped = VisualEvidenceFactoryConfig(
        output_dir=_build_root(),
        max_generation_retries=99,
    )
    assert 1 + clamped.max_generation_retries == 3
    single = VisualEvidenceFactoryConfig(
        output_dir=_build_root(),
        max_generation_retries=0,
    )
    assert single.max_generation_retries == 0


def test_budget_skip_preserves_existing_behavior() -> None:
    root = _build_root()
    path = _artifact("budget")
    generator = ScriptedGenerator(output_dir=root).add(_approved(path))
    factory = _factory(
        generator,
        cost_budget_cny=0.0,
        image_generation_reference_cost_cny=0.5,
    )
    try:
        generated, unresolved = factory._generated_figures(
            [_request("V-BUDGET")],
            blueprint={"sections": []},
        )
        assert generator.calls == []
        assert generated == []
        assert unresolved[0]["reason"] == (
            "visual_budget_exhausted_before_structured_fallback"
        )
    finally:
        path.unlink(missing_ok=True)


def test_max_generated_images_caps_tasks_not_successes() -> None:
    """First approved, second exhausted: third must never be attempted."""

    root = _build_root()
    first = _artifact("task1-ok")
    second_paths = [_artifact("task2-rev") for _ in range(3)]
    generator = ScriptedGenerator(output_dir=root).add(_approved(first))
    for path in second_paths:
        generator.add(_revision(path, ["Fix labels"]))
    factory = _factory(
        generator,
        max_generated_images=2,
        cost_budget_cny=10.0,
    )
    try:
        generated, unresolved = factory._generated_figures(
            [
                _request("V-TASKS-1"),
                _request("V-TASKS-2"),
                _request("V-TASKS-3"),
            ],
            blueprint={"sections": []},
        )
        assert len(generator.calls) == 4  # 1 approved + 3 exhausted
        # Slot accounting is unchanged by the salvage relaxation: two tasks
        # consume both slots and the third never reaches the generator.  Only
        # the disposition of the exhausted task changed -- its last rendered
        # attempt is now placed with disclosure instead of discarded.
        assert len(generated) == 2
        assert [row["salvaged_over_reviewer_objection"] for row in generated] == [
            False,
            True,
        ]
        assert len(unresolved) == 1
        assert unresolved[0]["reason"] == (
            "generation_task_budget_or_lower_priority"
        )
        assert all(
            call["visual_plan_id"] != "V-TASKS-3"
            for call in generator.calls
        )
    finally:
        first.unlink(missing_ok=True)
        for path in second_paths:
            path.unlink(missing_ok=True)


def test_both_failed_tasks_consume_slots_before_third() -> None:
    """Two exhausted tasks consume both slots; third never calls generator."""

    root = _build_root()
    generator = ScriptedGenerator(output_dir=root)
    paths: list[Path] = []
    for task_index in range(2):
        for _ in range(3):
            path = _artifact(f"fail-{task_index}")
            paths.append(path)
            generator.add(_revision(path, ["Fix labels"]))
    factory = _factory(
        generator,
        max_generated_images=2,
        cost_budget_cny=10.0,
    )
    try:
        generated, unresolved = factory._generated_figures(
            [
                _request("V-FAIL-1"),
                _request("V-FAIL-2"),
                _request("V-FAIL-3"),
            ],
            blueprint={"sections": []},
        )
        assert len(generator.calls) == 6  # 2 tasks * 3 attempts
        # Both exhausted tasks still consume a slot and the third is still
        # never generated -- the cap counts tasks, not successes.  Both
        # exhausted tasks rendered, so both are salvaged with disclosure.
        assert len(generated) == 2
        assert all(
            row["salvaged_over_reviewer_objection"] is True
            for row in generated
        )
        assert [row["reason"] for row in unresolved] == [
            "generation_task_budget_or_lower_priority",
        ]
        assert all(
            call["visual_plan_id"] != "V-FAIL-3"
            for call in generator.calls
        )
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


def test_max_zero_keeps_existing_semantics() -> None:
    root = _build_root()
    generator = ScriptedGenerator(output_dir=root)
    factory = _factory(generator, max_generated_images=0)
    requests = [_request("V-ZERO-1"), _request("V-ZERO-2")]
    generated, unresolved = factory._generated_figures(
        requests,
        blueprint={"sections": []},
    )
    assert generator.calls == []
    assert generated == []
    assert unresolved == [dict(row) for row in requests]
    assert "reason" not in unresolved[0]


def test_successful_task_still_consumes_slot_and_caps_next() -> None:
    root = _build_root()
    first = _artifact("ok-slot")
    generator = ScriptedGenerator(output_dir=root).add(_approved(first))
    factory = _factory(generator, max_generated_images=1)
    try:
        generated, unresolved = factory._generated_figures(
            [_request("V-OK-1"), _request("V-OK-2")],
            blueprint={"sections": []},
        )
        assert len(generator.calls) == 1
        assert len(generated) == 1
        assert len(unresolved) == 1
        assert unresolved[0]["reason"] == (
            "generation_task_budget_or_lower_priority"
        )
    finally:
        first.unlink(missing_ok=True)


class ThreadSafeScriptedGenerator:
    """Scripted generator that tracks cross-figure concurrency safely."""

    def __init__(self, *, output_dir: Path, **_: object) -> None:
        del output_dir
        self.calls: list[dict[str, Any]] = []
        self.results: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def add(self, result: dict[str, Any]) -> "ThreadSafeScriptedGenerator":
        self.results.append(result)
        return self

    def generate(self, *, plan: dict, section: dict) -> dict:
        del section
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            index = len(self.calls)
            self.calls.append(dict(plan))
        try:
            time.sleep(0.01)  # widen the overlap window for the test
            result = dict(
                self.results[
                    min(index, len(self.results) - 1)
                ]
            )
            local_path = result.get("local_image_path")
            if local_path:
                path = Path(str(local_path))
                if not path.is_file():
                    path.write_bytes(b"fake-image")
            return {**plan, **result}
        finally:
            with self.lock:
                self.active -= 1


class ExceptionThenApprovedGenerator(ThreadSafeScriptedGenerator):
    """Raises for one figure while a sibling figure can still succeed."""

    def __init__(self, *, output_dir: Path, fail_plan_id: str) -> None:
        super().__init__(output_dir=output_dir)
        self.fail_plan_id = fail_plan_id

    def generate(self, *, plan: dict, section: dict) -> dict:
        if str(plan.get("visual_plan_id") or "") == self.fail_plan_id:
            raise RuntimeError("scripted visual transport failure")
        return super().generate(plan=plan, section=section)


class RetryExceptionGenerator:
    """Succeeds up to the configured call index, then raises once."""

    def __init__(
        self,
        *,
        output_dir: Path,
        fail_call_index: int,
        **_: object,
    ) -> None:
        del output_dir
        self.calls: list[dict[str, Any]] = []
        self.results: list[dict[str, Any]] = []
        self.fail_call_index = fail_call_index

    def add(self, result: dict[str, Any]) -> "RetryExceptionGenerator":
        self.results.append(result)
        return self

    def generate(self, *, plan: dict, section: dict) -> dict:
        del section
        self.calls.append(dict(plan))
        index = len(self.calls) - 1
        if index == self.fail_call_index:
            raise RuntimeError("scripted retry transport failure")
        result = dict(
            self.results[min(index, len(self.results) - 1)]
        )
        local_path = result.get("local_image_path")
        if local_path:
            path = Path(str(local_path))
            if not path.is_file():
                path.write_bytes(b"fake-image")
        return {**plan, **result}


class ScriptedDiagramRenderer:
    """Fake structured-diagram renderer returning a ready local diagram."""

    def __init__(self, *, output_dir: Path, **_: object) -> None:
        del output_dir
        self.calls = 0
        self.paths: list[Path] = []

    def render(
        self, *, plan: dict, section: dict, figure_id: str
    ) -> dict[str, Any]:
        del plan, section
        self.calls += 1
        path = _artifact(f"fallback-{figure_id}")
        path.write_bytes(b"fake-diagram")
        provenance = path.with_suffix(".provenance.json")
        provenance.write_text(
            json.dumps({"source": "scripted-diagram-renderer"}),
            encoding="utf-8",
        )
        self.paths.append(path)
        return {
            "status": "ready",
            "local_image_path": str(path),
            "provenance_path": str(provenance),
            "spec": {
                "title": "Scripted fallback diagram",
                "takeaway": "Reader-facing explanatory structure.",
            },
            "model_usage": {},
        }


def test_generation_parallel_across_figures_serial_within_figure() -> None:
    root = _build_root()
    generator = ThreadSafeScriptedGenerator(output_dir=root)
    paths: list[Path] = []
    for plan_id in ("V-PAR-1", "V-PAR-2"):
        artifact = _artifact(f"{plan_id}-ok")
        paths.append(artifact)
        generator.add(_approved(artifact))
    factory = _factory(
        generator,
        max_generated_images=2,
        cost_budget_cny=10.0,
        workers=2,
    )
    try:
        generated, unresolved = factory._generated_figures(
            [_request("V-PAR-1"), _request("V-PAR-2")],
            blueprint={"sections": []},
        )
        # Cross-figure requests overlap; within a figure the sequence stays
        # serial (each figure issues exactly one approved attempt).
        assert generator.max_active >= 2
        assert [row["figure_id"] for row in generated] == [
            "V-PAR-1",
            "V-PAR-2",
        ]
        assert unresolved == []
        assert factory.cost["generated_figures"] == 2
        assert factory.cost["estimated_cost_cny"] <= 10.0
        assert factory.cost["estimated_cost_cny"] >= 0.0
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


def test_generation_worker_exception_is_nonblocking_unresolved() -> None:
    root = _build_root()
    ok_path = _artifact("worker-exception-ok")
    generator = ExceptionThenApprovedGenerator(
        output_dir=root, fail_plan_id="V-FAIL"
    ).add(_approved(ok_path))
    factory = _factory(
        generator,
        max_generated_images=2,
        cost_budget_cny=10.0,
        workers=2,
    )
    try:
        generated, unresolved = factory._generated_figures(
            [_request("V-FAIL"), _request("V-OK")],
            blueprint={"sections": []},
        )
        assert [row["figure_id"] for row in generated] == ["V-OK"]
        assert len(unresolved) == 1
        assert unresolved[0]["visual_plan_id"] == "V-FAIL"
        assert unresolved[0]["reason"] == "generation_worker_exception"
        assert any(
            event["event"] == "generated_visual_worker_failed"
            and event["visual_plan_id"] == "V-FAIL"
            for event in factory.events
        )
    finally:
        ok_path.unlink(missing_ok=True)


def test_budget_allows_only_one_concurrent_generation() -> None:
    """Atomic reservation prevents concurrent raster generations from over-
    spending; the second figure is admitted only for the cheaper structured
    fallback when its own allowance still fits.

    ``estimate_call_cost_cny`` is patched to return 0.0 so that all
    dynamic allowances collapse to their floor constants
    (_GENERATION_AUDIT_RESERVE_CNY=0.05, _STRUCTURED_SPEC_RESERVE_CNY=0.10)
    regardless of which Qwen pricing tables were loaded by earlier tests.
    Without the patch, a loaded pricing table can push the audit-reserve
    component to ~0.5 CNY, making budget=1.0 too tight for the structured
    fallback — Worker 2 then goes to unresolved instead of the renderer.
    """

    root = _build_root()
    generator = ThreadSafeScriptedGenerator(output_dir=root)
    paths: list[Path] = []
    plan_ids = [
        f"V-ONLY-{index}-{uuid.uuid4().hex[:8]}"
        for index in (1, 2)
    ]
    for plan_id in plan_ids:
        artifact = _artifact(f"{plan_id}-ok")
        paths.append(artifact)
        generator.add(_approved(artifact))
    # Budget fits exactly one generation allowance (reference cost 0.5 +
    # audit-reserve floor 0.05 = 0.55); two concurrent workers must never
    # both launch a raster generation.  The loser runs the structured
    # fallback (allowance = spec-floor 0.10 + audit-reserve floor 0.05 =
    # 0.15), which easily fits in the remaining 0.45 CNY budget.
    renderer = ScriptedDiagramRenderer(output_dir=root)

    def fake_vision(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {
            "content": json.dumps(
                {
                    "verdict": "approve",
                    "scientific_coherence": "high",
                    "label_legibility": "high",
                    "trend_direction_correct": True,
                    "misleading_elements": [],
                }
            ),
            "_llm_usage": {},
        }

    # Pin estimate_call_cost_cny → 0.0 so allowances are floor-only and
    # independent of whichever Qwen pricing config earlier tests may have
    # loaded into the module cache.
    with patch(
        "optomind_research.runtime.visual_evidence_factory"
        ".estimate_call_cost_cny",
        return_value=0.0,
    ):
        factory = _factory(
            generator,
            max_generated_images=2,
            cost_budget_cny=1.0,
            workers=2,
            diagram_renderer_factory=lambda **_: renderer,
            vision_call=fake_vision,
        )
        try:
            generated, unresolved = factory._generated_figures(
                [_request(plan_ids[0]), _request(plan_ids[1])],
                blueprint={"sections": []},
            )
            assert len(generator.calls) == 1
            assert renderer.calls == 1
            assert len(generated) == 2
            assert unresolved == []
            assert factory.cost["image_generation_calls"] == 1
            assert factory.cost["estimated_cost_cny"] <= 1.0
            # All reservations were reconciled: nothing stays reserved.
            assert factory.cost["reserved_generation_cost_cny"] == 0.0
            assert factory.reserved_generation_cost_cny == 0.0
            assert factory.generation_reservations >= 1
            assert (
                factory.cost["estimated_cost_cny"]
                + factory.cost["reserved_generation_cost_cny"]
                <= 1.0
            )
        finally:
            for path in renderer.paths:
                path.unlink(missing_ok=True)
                path.with_suffix(".provenance.json").unlink(missing_ok=True)
            for path in paths:
                path.unlink(missing_ok=True)


def test_generation_reservation_includes_model_review_budget() -> None:
    """Reservation must cover the post-generation vision review cost."""

    root = _build_root()
    generator = ThreadSafeScriptedGenerator(output_dir=root)
    paths: list[Path] = []
    for plan_id in ("V-REVIEW-1", "V-REVIEW-2"):
        artifact = _artifact(f"{plan_id}-ok")
        paths.append(artifact)
        generator.add(
            _approved_with_usage(
                artifact,
                model_name="unknown-future-model",
                input_tokens=64_000,
                output_tokens=1_200,
            )
        )
    factory = _factory(
        generator,
        max_generated_images=2,
        cost_budget_cny=1.4,
        vision_model_tier="unknown-future-model",
        workers=2,
    )
    try:
        generated, unresolved = factory._generated_figures(
            [_request("V-REVIEW-1"), _request("V-REVIEW-2")],
            blueprint={"sections": []},
        )
        assert len(generator.calls) == 1
        assert len(generated) == 1
        assert len(unresolved) == 1
        assert factory.cost["estimated_cost_cny"] <= 1.4
        assert factory.cost["vision_calls"] == 1
        assert factory.cost["reserved_generation_cost_cny"] == 0.0
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


def test_structured_fallback_uses_fallback_allowance_in_intermediate_band() -> None:
    """A remaining budget between fallback and raster allowances still admits
    the structured fallback instead of skipping it."""

    root = _build_root()
    generator = ScriptedGenerator(output_dir=root).add(
        {
            "generation_status": "generation_failed",
            "generation_error": "transport_boom",
            "model_review": {},
        }
    )
    renderer = ScriptedDiagramRenderer(output_dir=root)

    def fake_vision(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {
            "content": json.dumps(
                {
                    "verdict": "approve",
                    "scientific_coherence": "high",
                    "label_legibility": "high",
                    "trend_direction_correct": True,
                    "misleading_elements": [],
                }
            ),
            "_llm_usage": {},
        }

    probe = _factory(
        generator,
        max_generation_retries=0,
        cost_budget_cny=10.0,
        diagram_renderer_factory=lambda **_: renderer,
        vision_call=fake_vision,
    )
    fallback_allowance = probe._structured_fallback_allowance()
    generation_allowance = probe._generation_allowance()
    assert fallback_allowance < generation_allowance
    budget = round((fallback_allowance + generation_allowance) / 2, 6)

    factory = _factory(
        generator,
        max_generation_retries=0,
        cost_budget_cny=budget,
        diagram_renderer_factory=lambda **_: renderer,
        vision_call=fake_vision,
    )
    assert (
        factory._remaining_budget()
        >= factory._structured_fallback_allowance()
    )
    assert factory._remaining_budget() < factory._generation_allowance()
    plan_id = f"V-FALLBACK-BAND-{uuid.uuid4().hex[:8]}"
    try:
        generated, unresolved = factory._generated_figures(
            [_request(plan_id)],
            blueprint={"sections": []},
        )
        # Raster admission correctly refused: no raster call at all.
        assert generator.calls == []
        # Fallback admitted in the intermediate band: one local render.
        assert renderer.calls == 1
        assert unresolved == []
        assert len(generated) == 1
        assert generated[0]["figure_id"] == plan_id
        assert (
            generated[0]["generation_result"]["generation_status"]
            == "model_approved_human_pending"
        )
        assert any(
            event["event"] == "structured_diagram_fallback_started"
            for event in factory.events
        )
        assert not any(
            event["event"]
            == "structured_diagram_fallback_skipped_by_budget"
            for event in factory.events
        )
        assert factory.cost["reserved_generation_cost_cny"] == 0.0
        assert factory.cost["estimated_cost_cny"] <= budget
    finally:
        for path in renderer.paths:
            path.unlink(missing_ok=True)
            path.with_suffix(".provenance.json").unlink(missing_ok=True)


def test_retry_call_exception_preserves_previous_attempt() -> None:
    """A raising retry keeps the earlier attempt, history and spend audit."""

    root = _build_root()
    first = _artifact("retry-exc")
    generator = RetryExceptionGenerator(
        output_dir=root, fail_call_index=1
    ).add(_revision(first, ["Add clear axis labels"]))
    factory = _factory(generator, cost_budget_cny=10.0)
    try:
        generated, unresolved = factory._generated_figures(
            [_request("V-RETRY-EXC")],
            blueprint={"sections": []},
        )
        assert unresolved == []
        assert len(generated) == 1
        figure = generated[0]
        assert figure["salvaged_over_reviewer_objection"] is True
        result = figure["generation_result"]
        assert result["generation_attempts_exhausted"] is True
        # The previous attempt (image, review) is preserved, not discarded.
        assert result["local_image_path"] == str(first)
        assert result["model_review"]["verdict"] == "revise"
        assert result["generation_retry_error"].startswith(
            "RuntimeError:"
        )
        assert result["generation_retry_stop_reason"] == (
            "retry_call_failed"
        )
        assert result["generation_total_attempts"] == 2
        assert len(result["retry_history"]) == 1
        assert result["retry_history"][0]["retry_error"].startswith(
            "RuntimeError:"
        )
        assert len(generator.calls) == 2
        assert "Add clear axis labels" in generator.calls[1][
            "generation_brief"
        ]
        # Only the first attempt was billable; the failed retry added none.
        assert factory.cost["image_generation_calls"] == 1
        assert factory.cost["reserved_generation_cost_cny"] == 0.0
        assert any(
            event["event"] == "generation_retry_failed"
            and event["error"].startswith("RuntimeError:")
            for event in factory.events
        )
    finally:
        first.unlink(missing_ok=True)


def test_concurrent_source_audit_cost_mutations_are_consistent() -> None:
    """Source-figure audit counters stay exact under concurrent calls."""

    root = _build_root()
    paths = [_artifact(f"audit-{index}") for index in range(4)]
    for path in paths:
        path.write_bytes(b"fake-source-image")
    calls: list[Any] = []
    purpose = f"Explain the mechanism. {uuid.uuid4().hex}"

    def fake_vision(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        calls.append(True)
        return {
            "content": json.dumps(
                {
                    "verdict": "approve",
                    "section_fit": "direct",
                    "editorial_caption": "Caption.",
                }
            ),
            "_llm_usage": {
                "model_name": "unknown-future-model",
                "input_tokens": 10,
                "output_tokens": 5,
            },
        }

    factory = VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=root,
            real_visual_audit=True,
            real_image_generation=False,
            test_mode=True,
            cost_budget_cny=10.0,
        ),
        vision_call=fake_vision,
    )
    items = [
        {
            "status": "pending_generation_and_review",
            "local_image_path": str(path),
            "section_id": "S01",
            "argumentative_purpose": purpose,
            "source_caption": f"Fig {index}",
            "paper_id": f"paper-{index}",
            "visual_chunk_id": f"chunk-{index}",
            "figure_id": f"FIG-SRC-{index}",
        }
        for index, path in enumerate(paths)
    ]
    threads = [
        threading.Thread(
            target=factory._audit_selected_source, args=(item,)
        )
        for item in items
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(calls) == 4
        assert factory.cost["vision_calls"] == 4
        assert factory.cost["vision_input_tokens"] == 40
        assert factory.cost["vision_output_tokens"] == 20
        assert factory.cost["reserved_generation_cost_cny"] == 0.0
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


def _illegible(path: Path, feedback: list[str]) -> dict[str, Any]:
    """A rendered attempt whose text came out as garbage.

    Mirrors what qwen-image-2.0-pro produced for FIG-GEN-002 in the
    visA_stage2b run: a real PNG, a reviewer who named the garbling, and a
    ``label_legibility`` of "low".
    """

    return {
        "generation_status": "model_rejected_or_revision_required",
        "local_image_path": str(path),
        "provenance_path": str(path.with_suffix(".provenance.json")),
        "model_review": {
            "verdict": "revise",
            "required_revisions": feedback,
            "misleading_elements": ["Title text is garbled"],
            "label_legibility": "low",
        },
    }


def test_illegible_figure_is_not_salvaged_into_placement() -> None:
    root = _build_root()
    paths = [_artifact("illegible") for _ in range(3)]
    generator = ScriptedGenerator(output_dir=root)
    for index, path in enumerate(paths):
        generator.add(
            _illegible(path, [f"Fix garbled label {index + 1}"])
        )
    factory = _factory(generator)
    try:
        generated, unresolved = factory._generated_figures(
            [_request("V-ILLEGIBLE")],
            blueprint={"sections": []},
        )
        # The relaxation covers traceability and empirical grounding, not
        # legibility: a figure whose labels are unreadable cannot support the
        # caption written for it, so it must not reach placement even though
        # a renderable file exists.
        assert generated == []
        assert len(unresolved) == 1
        assert unresolved[0]["reason"] == "generated_visual_illegible"
        # Retry behaviour upstream is untouched -- still three feedback
        # carrying attempts before the figure is dropped.
        assert len(generator.calls) == MAX_GENERATION_TOTAL_ATTEMPTS == 3
        assert all(path.is_file() for path in paths)
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


def test_legible_objection_is_still_salvaged() -> None:
    root = _build_root()
    paths = [_artifact("legible-objection") for _ in range(3)]
    generator = ScriptedGenerator(output_dir=root)
    for index, path in enumerate(paths):
        generator.add(_revision(path, [f"Tighten wording {index + 1}"]))
    factory = _factory(generator)
    try:
        generated, unresolved = factory._generated_figures(
            [_request("V-LEGIBLE")],
            blueprint={"sections": []},
        )
        # Guards the other half of the boundary: without a legibility
        # failure the salvage path must keep placing the last attempt, so
        # the new gate cannot quietly become a blanket reviewer veto.
        assert unresolved == []
        assert len(generated) == 1
        assert generated[0]["salvaged_over_reviewer_objection"] is True
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
