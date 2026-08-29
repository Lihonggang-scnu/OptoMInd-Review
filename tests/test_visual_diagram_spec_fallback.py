"""P1-3 regression tests: overflow visibility and explicit degradation.

Covers the two P1-3 factory changes plus the evaluator honesty change:
  1. requests absorbed by max_generated_images now emit per-request
     overflow events carrying the request total and the cap;
  2. attempts exhausted after feedback-carrying retries carry an
     explicit degradation note (reason enum value untouched);
  3. exhaustion without any feedback retry carries no note;
  4. max_generated_images default stays 2;
  5. conceptual_visual_request_count is None in final-package mode
     and still counted in editorial-plan mode.

No model, network, or image API is touched; placeholder files live
under build/ like the neighbouring factory tests.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from optomind_research.runtime.review_content_evaluator import (
    evaluate_review_content,
)
from optomind_research.runtime.visual_evidence_factory import (
    VisualEvidenceFactory,
    VisualEvidenceFactoryConfig,
)


def _build_root() -> Path:
    root = Path.cwd() / "build"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _artifact(prefix: str) -> Path:
    return _build_root() / ("p13-" + prefix + "-" + uuid.uuid4().hex[:10] + ".png")


def _request(plan_id: str, section_id: str = "S01") -> dict[str, Any]:
    return {
        "visual_plan_id": plan_id,
        "section_id": section_id,
        "figure_kind": "mechanism_schematic",
        "argumentative_purpose": "Explain the mechanism.",
        "generation_brief": "Draw it.",
        "data_provenance_level": "schematic",
        "status": "pending_generation_and_review",
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


def _revision(path: Path, feedback: str) -> dict[str, Any]:
    return {
        "generation_status": "model_rejected_or_revision_required",
        "local_image_path": str(path),
        "provenance_path": str(path.with_suffix(".provenance.json")),
        "model_review": {
            "verdict": "revise",
            "required_revisions": [feedback],
            "misleading_elements": ["Legend unclear"],
        },
    }


class ScriptedGenerator:
    def __init__(self, *, output_dir: Path, **_: object) -> None:
        del output_dir
        self.calls = []
        self.results = []

    def add(self, result):
        self.results.append(result)
        return self

    def generate(self, *, plan, section):
        del section
        self.calls.append(dict(plan))
        result = dict(
            self.results[min(len(self.calls) - 1, len(self.results) - 1)]
        )
        local_path = result.get("local_image_path")
        if local_path:
            path = Path(str(local_path))
            if not path.is_file():
                path.write_bytes(b"fake-image")
        return {**plan, **result}


def _factory(generator, **overrides):
    config_kwargs = {
        "output_dir": _build_root(),
        "real_image_generation": True,
        "test_mode": True,
        "workers": 1,
    }
    config_kwargs.update(overrides)
    return VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(**config_kwargs),
        conceptual_generator_factory=lambda **_: generator,
    )

def test_overflow_requests_emit_gap_events() -> None:
    root = _build_root()
    paths = [_artifact("ovf-a"), _artifact("ovf-b")]
    generator = ScriptedGenerator(output_dir=root)
    for path in paths:
        generator.add(_approved(path))
    factory = _factory(generator, max_generated_images=2)
    try:
        generated, unresolved = factory._generated_figures(
            [
                _request("V-OVF-1", section_id="S08"),
                _request("V-OVF-2", section_id="S01"),
                _request("V-OVF-3", section_id="S07"),
            ],
            blueprint={"sections": []},
        )
        assert len(generated) == 2
        assert len(unresolved) == 1
        assert unresolved[0]["reason"] == "generation_task_budget_or_lower_priority"
        assert unresolved[0]["section_id"] == "S07"
        overflows = [
            row
            for row in factory.events
            if row["event"] == "conceptual_visual_generation_overflow"
        ]
        assert len(overflows) == 1
        event = overflows[0]
        assert event["section_id"] == "S07"
        assert event["total_conceptual_requests"] == 3
        assert event["max_generated_images"] == 2
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


def test_exhausted_after_feedback_retries_carries_degradation_note() -> None:
    root = _build_root()
    paths = [_artifact("deg") for _ in range(3)]
    generator = ScriptedGenerator(output_dir=root)
    for index, path in enumerate(paths):
        generator.add(_revision(path, "Fix issue " + str(index + 1)))
    factory = _factory(generator, max_generated_images=2)
    try:
        generated, unresolved = factory._generated_figures(
            [_request("V-DEG")],
            blueprint={"sections": []},
        )
        # Private-study relaxation: every attempt rendered, so the last one
        # is salvaged rather than discarded.  The degradation note must
        # survive onto the placed figure -- it is the only record of how many
        # feedback rounds the figure failed before being kept anyway.
        assert unresolved == []
        assert len(generated) == 1
        figure = generated[0]
        assert figure["salvaged_over_reviewer_objection"] is True
        note = figure["generation_degradation_note"]
        assert "feedback-carrying retries" in note
        ready_events = [
            row
            for row in factory.events
            if row["event"] == "conceptual_visual_ready"
        ]
        assert len(ready_events) == 1
        assert "feedback-carrying retries" in ready_events[0][
            "generation_degradation_note"
        ]
        assert ready_events[0]["salvaged_over_reviewer_objection"] is True
        # A plain "revise" verdict is a quality objection, not an integrity
        # one, so no caption warning is injected.
        assert figure["integrity_flags"] == []
        assert "WARNING" not in figure["caption_en"]
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


def test_integrity_flagged_salvage_carries_caption_warning() -> None:
    """A figure kept over a fabrication finding must say so in its caption.

    The user chose literal last-attempt retention with explicit risk
    disclosure.  The warning therefore lives in ``caption_en`` itself, not in
    a side-channel field, so it survives any renderer that only reads the
    caption.
    """

    root = _build_root()
    path = _artifact("integrity")
    generator = ScriptedGenerator(output_dir=root)
    for index in range(3):
        generator.add(
            {
                "generation_status": "model_rejected_or_revision_required",
                "local_image_path": str(path),
                "model_review": {
                    "verdict": "reject",
                    "contains_fabricated_empirical_content": True,
                    # label_legibility is deliberately absent.  This fixture
                    # started as a kitchen-sink rejection, but illegibility
                    # now blocks placement outright, which would stop this
                    # figure before the caption is ever built and turn a
                    # fabrication-disclosure test into a legibility test.
                    # The intersection of the two rules has its own test
                    # below.
                    "scientific_coherence": "low",
                    "trend_direction_correct": True,
                    "misleading_elements": ["invented efficiency values"],
                },
            }
        )
    factory = _factory(generator, max_generated_images=2)
    try:
        generated, unresolved = factory._generated_figures(
            [_request("V-INTEG")],
            blueprint={"sections": []},
        )
        assert unresolved == []
        assert len(generated) == 1
        figure = generated[0]
        assert figure["salvaged_over_reviewer_objection"] is True
        assert figure["integrity_flags"] == ["fabricated_empirical_content"]
        caption = figure["caption_en"]
        assert "WARNING" in caption
        assert "fabricated empirical content" in caption
        assert "do not cite" in caption
    finally:
        path.unlink(missing_ok=True)


def test_submission_profile_hard_blocks_integrity_flags() -> None:
    root = _build_root()
    path = _artifact("submission-integrity")
    generator = ScriptedGenerator(output_dir=root)
    generator.add(
        {
            "generation_status": "model_rejected_or_revision_required",
            "local_image_path": str(path),
            "model_review": {
                "verdict": "reject",
                "contains_fabricated_empirical_content": True,
                "fake_paper_attribution": True,
                "trend_direction_correct": False,
            },
        }
    )
    factory = _factory(
        generator,
        max_generated_images=1,
        max_generation_retries=0,
        execution_profile="submission",
    )
    try:
        generated, unresolved = factory._generated_figures(
            [_request("V-SUBMISSION-INTEGRITY")],
            blueprint={"sections": []},
        )
        assert generated == []
        assert len(unresolved) == 1
        assert unresolved[0]["reason"] == "submission_integrity_blocked"
        assert unresolved[0]["generation_result"]["model_review"][
            "contains_fabricated_empirical_content"
        ]
    finally:
        path.unlink(missing_ok=True)


def test_illegible_takes_precedence_over_the_disclosure_policy() -> None:
    """Where the two relaxation rules collide, illegibility wins.

    The disclosure policy says every reviewer veto -- fabrication included
    -- downgrades to placement plus a caption warning.  The legibility gate
    says an illegible figure never reaches placement.  A figure that is both
    fabricated and unreadable satisfies neither rule cleanly, so the
    precedence is pinned here rather than left to gate ordering.

    Illegibility wins because the disclosure policy exists to keep figures a
    strict reviewer would have discarded but that are nonetheless usable --
    an untraceable figure can still be a correct one.  A figure whose every
    label is garbled is not in that category: placing it satisfies the
    letter of "place with disclosure" while defeating its purpose, since
    neither the caption's description nor its warning refers to anything the
    reader can actually see.
    """

    root = _build_root()
    path = _artifact("integrity-illegible")
    generator = ScriptedGenerator(output_dir=root)
    for _ in range(3):
        generator.add(
            {
                "generation_status": "model_rejected_or_revision_required",
                "local_image_path": str(path),
                "model_review": {
                    "verdict": "reject",
                    "contains_fabricated_empirical_content": True,
                    "label_legibility": "low",
                    "misleading_elements": ["invented values, and all text is garbled"],
                },
            }
        )
    factory = _factory(generator, max_generated_images=2)
    try:
        generated, unresolved = factory._generated_figures(
            [_request("V-INTEG-ILLEGIBLE")],
            blueprint={"sections": []},
        )
        assert generated == []
        assert len(unresolved) == 1
        assert unresolved[0]["reason"] == "generated_visual_illegible"
    finally:
        path.unlink(missing_ok=True)


def test_unrenderable_last_attempt_is_still_unresolved() -> None:
    """Relaxation covers reviewer objections, not missing files.

    Without this the salvage branch would silently place a figure whose
    image never materialized, and the unresolved-need channel would go
    permanently empty.
    """

    generator = ScriptedGenerator(output_dir=_build_root()).add(
        {
            "generation_status": "model_rejected_or_revision_required",
            "local_image_path": "",
            "model_review": {"verdict": "reject"},
        }
    )
    factory = _factory(
        generator,
        max_generated_images=2,
        real_image_generation=False,
    )
    generated, unresolved = factory._generated_figures(
        [_request("V-NOFILE")],
        blueprint={"sections": []},
    )
    assert generated == []
    assert len(unresolved) == 1



def test_no_feedback_retry_means_no_degradation_note() -> None:
    root = _build_root()
    generator = ScriptedGenerator(output_dir=root).add(
        {
            # No image, no review verdict: lands in unresolved with a
            # status-based reason and zero feedback-carrying retries.
            "generation_status": "",
            "local_image_path": "",
            "model_review": {},
        }
    )
    # real_image_generation=False disables the structured-fallback rescue
    # so the bare result reaches the unresolved branch untouched.
    factory = _factory(
        generator,
        max_generated_images=2,
        real_image_generation=False,
    )
    generated, unresolved = factory._generated_figures(
        [_request("V-NOFB")],
        blueprint={"sections": []},
    )
    assert generated == []
    assert len(unresolved) == 1
    assert "generation_degradation_note" not in unresolved[0]


def test_max_generated_images_default_matches_editor_request_cap() -> None:
    """The factory cap must not silently discard requests the editor may make.

    ``MAX_CONCEPTUAL_FIGURE_REQUESTS`` is the editor's ceiling; a lower
    factory cap dropped the trailing requests as
    ``generation_task_budget_or_lower_priority`` with no reviewer objection
    at all (be780761: S04 and S05).
    """

    from optomind_research.runtime.visual_editor_tool_provider import (
        MAX_CONCEPTUAL_FIGURE_REQUESTS,
    )

    config = VisualEvidenceFactoryConfig(output_dir=_build_root())
    assert config.max_generated_images == MAX_CONCEPTUAL_FIGURE_REQUESTS


def test_request_count_none_in_final_mode_and_int_in_plan_mode(tmp_path) -> None:
    final_package = tmp_path / "FINAL_VISUAL_PACKAGE.json"
    final_package.write_text(
        json.dumps({
            "figures": [{"local_image_path": "build/whatever.png"}],
            "unfilled_visual_opportunities": [
                {"section_id": "S05", "reason": "no_traceable_source_figures"},
                {"section_id": "S07", "reason": "generation_task_budget_or_lower_priority"},
            ],
        }),
        encoding="utf-8",
    )
    report = evaluate_review_content(
        final_review_path=None,
        blueprint={"sections": []},
        visual_plan_path=final_package,
        citation_map_path=None,
        output_dir=tmp_path / "out-final",
    )
    metrics = report["metrics"]
    assert metrics["visual_input_contract"] == "final_visual_package"
    assert metrics["conceptual_visual_request_count"] is None

    plan_file = tmp_path / "VISUAL_EDITORIAL_PLAN.json"
    plan_file.write_text(
        json.dumps({
            "placements": [],
            "conceptual_figure_requests": [
                _request("V-CNT-1"),
                _request("V-CNT-2", section_id="S04"),
            ],
            "unfilled_visual_needs": [],
        }),
        encoding="utf-8",
    )
    report_plan = evaluate_review_content(
        final_review_path=None,
        blueprint={"sections": []},
        visual_plan_path=plan_file,
        citation_map_path=None,
        output_dir=tmp_path / "out-plan",
    )
    assert report_plan["metrics"]["conceptual_visual_request_count"] == 2
