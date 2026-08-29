"""Revision-capable structured diagram renderer tests."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest

from optomind_research.runtime.conceptual_diagram_renderer import (
    ConceptualDiagramRenderer,
)

_TEMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp-diagram-revision-tests"


@pytest.fixture()
def work_dir() -> Path:
    _TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TEMP_ROOT / f"run-{uuid.uuid4().hex[:12]}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _valid_spec() -> dict:
    return {
        "title": "Optical sensing mechanism",
        "layout": "left_to_right",
        "nodes": [
            {"id": "N1", "label": "Incident light", "kind": "input"},
            {"id": "N2", "label": "Resonant cavity", "kind": "mechanism"},
            {"id": "N3", "label": "Field confinement", "kind": "mechanism"},
            {"id": "N4", "label": "Sensing readout", "kind": "outcome"},
        ],
        "edges": [
            {"source": "N1", "target": "N2", "label": "excites"},
            {"source": "N2", "target": "N3", "label": "enhances"},
            {"source": "N3", "target": "N4", "label": "enables"},
        ],
        "takeaway": (
            "Cavity confinement connects incident light to sensing readout."
        ),
    }


def _shallow_spec() -> dict:
    """One root fanning out to five siblings -- wide and only two ranks deep.

    This is the shape the S08 taxonomy request produced in visA_stage2b, and
    the shape that exposed the fixed-canvas whitespace.
    """

    return {
        "title": "Application taxonomy",
        "layout": "left_to_right",
        "nodes": [
            {"id": "N1", "label": "Passive radiative cooling", "kind": "input"},
            {"id": "N2", "label": "Building envelopes", "kind": "outcome"},
            {"id": "N3", "label": "Photovoltaic thermal", "kind": "outcome"},
            {"id": "N4", "label": "Wearable textiles", "kind": "outcome"},
            {"id": "N5", "label": "Water harvesting", "kind": "outcome"},
            {"id": "N6", "label": "Agricultural protection", "kind": "outcome"},
        ],
        "edges": [
            {"source": "N1", "target": "N2", "label": "lowest integration cost"},
            {"source": "N1", "target": "N3", "label": "raises cell efficiency"},
            {"source": "N1", "target": "N4", "label": "improves comfort"},
            {"source": "N1", "target": "N5", "label": "drives condensation"},
            {"source": "N1", "target": "N6", "label": "reduces heat stress"},
        ],
        "takeaway": (
            "Building envelopes lead adoption across cooling applications."
        ),
    }


def test_render_explicit_spec_locally_and_labels_remain_exact(
    work_dir: Path,
) -> None:
    renderer = ConceptualDiagramRenderer(
        output_dir=work_dir / "explicit",
        real_llm=False,
    )
    result = renderer.render_explicit_spec(
        spec=_valid_spec(),
        figure_id="explicit-01",
        plan={"figure_kind": "mechanism_schematic"},
        section={"title": "Optical mechanism"},
    )
    assert result["status"] == "ready"
    assert Path(result["local_image_path"]).is_file()
    assert result["spec"]["nodes"][1]["label"] == "Resonant cavity"
    assert result["spec"]["edges"][1]["label"] == "enhances"
    provenance = json.loads(
        Path(result["provenance_path"]).read_text(encoding="utf-8")
    )
    assert provenance["spec_origin"] == "explicit"


def test_revision_prompt_contains_old_spec_and_feedback(work_dir: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_llm(name: str, messages: list[dict], **_: Any) -> dict:
        del name
        captured["messages"] = messages
        revised = _valid_spec()
        revised["nodes"][2]["label"] = "Tight field confinement"
        revised["internal_note"] = "must be stripped locally"
        return {
            "content": json.dumps(revised),
            "_llm_usage": {
                "input_tokens": 120,
                "output_tokens": 40,
            },
        }

    renderer = ConceptualDiagramRenderer(
        output_dir=work_dir / "revision",
        real_llm=True,
        llm_call=fake_llm,
    )
    feedback = (
        "Make the field confinement node more explicit; preserve all "
        "other labels exactly."
    )
    result = renderer.revise_spec(
        previous_spec=_valid_spec(),
        reviewer_feedback=feedback,
        plan={"figure_kind": "mechanism_schematic"},
        section={"title": "Optical mechanism"},
        figure_id="rev-01",
    )
    assert result["status"] == "ready"
    assert Path(result["local_image_path"]).is_file()
    user_content = captured["messages"][1]["content"]
    assert "previous_spec" in user_content
    assert "N1" in user_content
    assert "Incident light" in user_content
    assert "reviewer_feedback" in user_content
    assert feedback in user_content
    assert set(result["spec"].keys()) == {
        "title",
        "layout",
        "nodes",
        "edges",
        "takeaway",
    }
    assert result["spec"]["nodes"][2]["label"] == "Tight field confinement"
    assert result["spec"]["nodes"][0]["label"] == "Incident light"
    provenance = json.loads(
        Path(result["provenance_path"]).read_text(encoding="utf-8")
    )
    assert provenance["spec_origin"] == "revision"
    assert provenance["reviewer_feedback"] == feedback
    assert provenance["previous_spec"]["nodes"][0]["label"] == (
        "Incident light"
    )


def test_malformed_revised_spec_fails_closed(work_dir: Path) -> None:
    def fake_llm(name: str, messages: list[dict], **_: Any) -> dict:
        del name, messages
        return {
            "content": "not valid json",
            "_llm_usage": {"input_tokens": 10, "output_tokens": 2},
        }

    renderer = ConceptualDiagramRenderer(
        output_dir=work_dir / "bad-revision",
        real_llm=True,
        llm_call=fake_llm,
    )
    result = renderer.revise_spec(
        previous_spec=_valid_spec(),
        reviewer_feedback="Make it clearer.",
        plan={},
        section={},
        figure_id="rev-bad",
    )
    assert result["status"] == "spec_invalid"
    assert result["error"] == "revised_spec_invalid"
    assert not (work_dir / "bad-revision" / "rev-bad.png").exists()


def test_deterministic_failure_when_llm_raises(work_dir: Path) -> None:
    def boom(name: str, messages: list[dict], **_: Any) -> dict:
        del name, messages
        raise RuntimeError("transport down")

    renderer = ConceptualDiagramRenderer(
        output_dir=work_dir / "failure",
        real_llm=True,
        llm_call=boom,
    )
    result = renderer.revise_spec(
        previous_spec=_valid_spec(),
        reviewer_feedback="Make it clearer.",
        plan={},
        section={},
        figure_id="rev-fail",
    )
    assert result["status"] == "revision_failed"
    assert "RuntimeError" in result["error"]


def test_local_validation_limits_ascii_and_rejects_quantitative_claims(
    work_dir: Path,
) -> None:
    renderer = ConceptualDiagramRenderer(
        output_dir=work_dir / "validation",
        real_llm=False,
    )
    oversized = _valid_spec()
    oversized["nodes"].extend(
        [
            {"id": "N5", "label": "Extra node five", "kind": "evidence"},
            {"id": "N6", "label": "Extra node six", "kind": "evidence"},
            {"id": "N7", "label": "Extra node seven", "kind": "evidence"},
            {"id": "N8", "label": "Extra node eight", "kind": "evidence"},
        ]
    )
    oversized["edges"].extend(
        [
            {"source": "N4", "target": f"N{index}", "label": "supports"}
            for index in range(5, 13)
        ]
    )
    oversized["nodes"][1]["label"] = "Resonant cavity 10 nm"
    result = renderer.render_explicit_spec(
        spec=oversized,
        figure_id="oversized",
    )
    assert result["status"] == "spec_invalid"

    ascii_spec = _valid_spec()
    ascii_spec["nodes"][1]["label"] = "2D material"
    ascii_result = renderer.render_explicit_spec(
        spec=ascii_spec,
        figure_id="ascii-ok",
    )
    assert ascii_result["status"] == "ready"
    assert ascii_result["spec"]["nodes"][1]["label"] == "2D material"


def test_existing_render_remains_compatible(work_dir: Path) -> None:
    renderer = ConceptualDiagramRenderer(
        output_dir=work_dir / "compat",
        real_llm=False,
    )
    result = renderer.render(
        plan={
            "figure_kind": "mechanism_schematic",
            "argumentative_purpose": "Explain an optical mechanism.",
        },
        section={"title": "Optical mechanism"},
        figure_id="compat-01",
    )
    assert result["status"] == "ready"
    assert Path(result["local_image_path"]).is_file()
    assert len(result["spec"]["nodes"]) >= 3
    assert all(node["label"] for node in result["spec"]["nodes"])


def test_revision_unavailable_without_real_llm(work_dir: Path) -> None:
    renderer = ConceptualDiagramRenderer(
        output_dir=work_dir / "no-llm",
        real_llm=False,
    )
    result = renderer.revise_spec(
        previous_spec=_valid_spec(),
        reviewer_feedback="Make it clearer.",
        plan={},
        section={},
        figure_id="rev-offline",
    )
    assert result["status"] == "revision_unavailable"


def test_canvas_height_follows_the_diagram_instead_of_a_fixed_1080(
    work_dir: Path,
) -> None:
    """A shallow diagram must not ship inside a mostly-empty canvas.

    graphviz sizes its PNG tightly to the graph, so the wide shallow shape a
    taxonomy or comparison spec produces arrived around 1500x420.  Pasting
    that at y=28 on the old fixed 1600x1080 canvas left over 600px of white
    above the footer -- more than half the delivered image.  Both generated
    diagrams in the visA_stage2b run looked broken for exactly this reason.
    """

    from PIL import Image

    renderer = ConceptualDiagramRenderer(
        output_dir=work_dir / "canvas",
        real_llm=False,
    )
    result = renderer.render_explicit_spec(
        spec=_shallow_spec(),
        figure_id="canvas-01",
        plan={"figure_kind": "taxonomy_diagram"},
        section={"title": "Application taxonomy"},
    )
    assert result["status"] == "ready"
    with Image.open(Path(result["local_image_path"])) as image:
        width, height = image.size
        # Width and the footer band are deliberately unchanged; only the dead
        # vertical space goes away.
        assert width == 1600
        assert height < 1080
        # The rule must travel with the canvas rather than being orphaned at
        # a hard-coded y=1010: a fixed offset would land outside a shorter
        # canvas and silently drop the disclosure band.
        rule_row = [
            image.getpixel((x, height - 70))
            for x in range(60, 1540, 40)
        ]
        assert all(sum(pixel[:3]) < 720 for pixel in rule_row)
        # And the band below the rule has to contain the label's dark pixels.
        label_pixels = sum(
            1
            for x in range(600, 1000)
            for y in range(height - 60, height - 40)
            if sum(image.getpixel((x, y))[:3]) < 400
        )
        assert label_pixels > 0


def test_tall_diagram_keeps_the_previous_canvas_height(
    work_dir: Path,
) -> None:
    """The other half of the boundary: deep graphs are unaffected.

    A four-node chain fills the (1500, 960) content cap, so the canvas lands
    at 960 + 28 + 92 = 1080 -- pixel-for-pixel what the fixed canvas produced,
    footer rule and label included.  Without this guard the fix could be
    "improved" into a general shrink that starts cropping deep diagrams.
    """

    from PIL import Image

    renderer = ConceptualDiagramRenderer(
        output_dir=work_dir / "tall",
        real_llm=False,
    )
    result = renderer.render_explicit_spec(
        spec=_valid_spec(),
        figure_id="tall-01",
        plan={"figure_kind": "mechanism_schematic"},
        section={"title": "Optical mechanism"},
    )
    assert result["status"] == "ready"
    with Image.open(Path(result["local_image_path"])) as image:
        assert image.size == (1600, 1080)
        # The footer must sit at the original hard-coded offsets, not merely
        # somewhere inside a 1080-tall canvas -- that is what makes the tall
        # case a true no-op rather than a coincidence of total height.
        rule_row = [image.getpixel((x, 1010)) for x in range(60, 1540, 40)]
        assert all(sum(pixel[:3]) < 720 for pixel in rule_row)
        label_pixels = sum(
            1
            for x in range(600, 1000)
            for y in range(1020, 1040)
            if sum(image.getpixel((x, y))[:3]) < 400
        )
        assert label_pixels > 0
