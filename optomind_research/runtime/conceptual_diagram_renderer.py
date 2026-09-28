"""Render reliable scientific concept diagrams from a constrained JSON spec."""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from PIL import Image, ImageDraw, ImageOps

from llm.qwen_chat_client import call_qwen_chat

from .artifact_store import atomic_write_json


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = (
    PROJECT_ROOT / "prompts" / "Conceptual Diagram Spec Generator.txt"
)
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
MAX_SEMANTIC_REVISIONS = 3
_QUANTITATIVE_CLAIM = re.compile(
    r"\b\d+(?:\.\d+)?\s*"
    r"(?:nm|um|μm|mm|cm|km|ms|us|μs|s|min|h|hz|khz|mhz|ghz|thz|%|db|"
    r"mv|kv|v|ma|a|mw|w|ev|mev|kev|k|c|f|g)\b",
    re.IGNORECASE,
)


def _find_graphviz_dot() -> str:
    """Find Graphviz even when a long-lived parent has a stale PATH.

    Windows installers commonly update the user PATH after the current Python
    process was launched.  The harness should still discover the canonical
    installation directory instead of reporting a false capability failure.
    """

    discovered = shutil.which("dot")
    candidates: list[Path] = []
    if discovered:
        candidates.append(Path(discovered))
    if os.name == "nt":
        for root_name in ("ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(root_name, "")
            if root:
                candidates.append(Path(root) / "Graphviz" / "bin" / "dot.exe")
        candidates.extend(
            [
                Path("C:/Program Files/Graphviz/bin/dot.exe"),
                Path("C:/Program Files (x86)/Graphviz/bin/dot.exe"),
                Path("C:/ProgramData/chocolatey/bin/dot.exe"),
            ]
        )
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return ""


def _safe_json(text: str) -> Dict[str, Any]:
    try:
        value = json.loads(str(text or ""))
        return value if isinstance(value, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", str(text or ""), re.S)
        if not match:
            return {}
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}


def _label(value: Any, *, max_words: int = 8) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = _CJK.sub("", text)
    text = re.sub(r"[^A-Za-z0-9+\-–—/()., α-ωΑ-Ω]", "", text)
    # A second canonicalization pass prevents transport mojibake from becoming
    # diagram labels even if an older permissive pattern is present above.
    text = re.sub(r"[^\x20-\x7E]", "", text)
    text = re.sub(r"[^A-Za-z0-9+\-^().,%/ ]", "", text)
    return " ".join(text.split()[:max_words]).strip()


def _validate_spec(value: Dict[str, Any]) -> Dict[str, Any]:
    raw_nodes = value.get("nodes") or []
    nodes = []
    seen = set()
    for index, row in enumerate(raw_nodes[:6], 1):
        if not isinstance(row, dict):
            continue
        node_id = re.sub(
            r"[^A-Za-z0-9_]+",
            "",
            str(row.get("id") or f"N{index}"),
        )
        label = _label(row.get("label"), max_words=5)
        if not node_id or not label or node_id in seen:
            continue
        seen.add(node_id)
        nodes.append(
            {
                "id": node_id,
                "label": label,
                "kind": str(row.get("kind") or "mechanism"),
            }
        )
    edges = []
    for row in (value.get("edges") or [])[:10]:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "")
        target = str(row.get("target") or "")
        if source in seen and target in seen and source != target:
            edges.append(
                {
                    "source": source,
                    "target": target,
                    "label": _label(row.get("label"), max_words=4),
                }
            )
    if len(nodes) < 3 or len(edges) < 2:
        return {}
    quantitative_texts = [
        _label(value.get("title"), max_words=10),
        _label(value.get("takeaway"), max_words=24),
    ]
    quantitative_texts.extend(node["label"] for node in nodes)
    quantitative_texts.extend(edge["label"] for edge in edges)
    if any(_QUANTITATIVE_CLAIM.search(text) for text in quantitative_texts):
        return {}
    layout = str(value.get("layout") or "left_to_right")
    if layout not in {"left_to_right", "top_to_bottom", "radial"}:
        layout = "left_to_right"
    return {
        "title": _label(value.get("title"), max_words=10)
        or "Scientific mechanism",
        "layout": layout,
        "nodes": nodes,
        "edges": edges,
        "takeaway": _label(value.get("takeaway"), max_words=24),
    }


class ConceptualDiagramRenderer:
    def __init__(
        self,
        *,
        output_dir: Path,
        real_llm: bool,
        model_tier: str = "standard_model",
        llm_call: Callable[..., Dict[str, Any]] = call_qwen_chat,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.real_llm = bool(real_llm)
        self.model_tier = model_tier
        self.llm_call = llm_call

    def _fallback_spec(self) -> Dict[str, Any]:
        return {
            "title": "Mechanism to application pathway",
            "layout": "left_to_right",
            "nodes": [
                {"id": "N1", "label": "Physical input", "kind": "input"},
                {"id": "N2", "label": "Optical mechanism", "kind": "mechanism"},
                {"id": "N3", "label": "Design trade-off", "kind": "tradeoff"},
                {"id": "N4", "label": "Application outcome", "kind": "outcome"},
            ],
            "edges": [
                {"source": "N1", "target": "N2", "label": "drives"},
                {"source": "N2", "target": "N3", "label": "constrains"},
                {"source": "N3", "target": "N4", "label": "enables"},
            ],
            "takeaway": "Mechanism and design constraints jointly determine application performance.",
        }

    def _build_spec(
        self,
        *,
        plan: Dict[str, Any],
        section: Dict[str, Any],
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        if not self.real_llm:
            return self._fallback_spec(), {}
        payload = {
            "figure_kind": plan.get("figure_kind", ""),
            "scientific_purpose": plan.get(
                "argumentative_purpose",
                plan.get("argument_role", ""),
            ),
            "generation_brief": plan.get("generation_brief", ""),
            "section_title": section.get(
                "section_title",
                section.get("title", ""),
            ),
            "section_argument": section.get(
                "chapter_argument",
                section.get("argument_role", ""),
            ),
            "data_provenance_level": plan.get(
                "data_provenance_level",
                "schematic",
            ),
        }
        try:
            response = self.llm_call(
                "ConceptualDiagramSpecGenerator",
                [
                    {
                        "role": "system",
                        "content": PROMPT_PATH.read_text(encoding="utf-8"),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                ],
                model_tier=self.model_tier,
                max_retries=1,
                temperature=0,
                max_tokens=900,
                response_format={"type": "json_object"},
                force_mock=False,
                timeout_seconds=120,
                max_transport_key_candidates=2,
                allow_model_fallback=True,
            )
        except Exception:
            return {}, {}
        spec = _validate_spec(
            _safe_json(str(response.get("content") or ""))
        )
        return spec, dict(response.get("_llm_usage") or {})

    @staticmethod
    def _compact_spec_table(spec: Dict[str, Any]) -> str:
        """Render a spec as compact table-like lines for a revision prompt."""

        lines = [
            f"title: {_label(spec.get('title'), max_words=10)}",
            f"layout: {spec.get('layout') or 'left_to_right'}",
            "nodes:",
        ]
        for index, node in enumerate(spec.get("nodes") or [], 1):
            lines.append(
                f"{index}. id={node.get('id', '')} | "
                f"label={node.get('label', '')} | "
                f"kind={node.get('kind', '')}"
            )
        lines.append("edges:")
        for index, edge in enumerate(spec.get("edges") or [], 1):
            lines.append(
                f"{index}. {edge.get('source', '')} -> "
                f"{edge.get('target', '')} | "
                f"label={edge.get('label', '')}"
            )
        lines.append(f"takeaway: {_label(spec.get('takeaway'), max_words=24)}")
        return "\n".join(lines)

    def _render_spec(
        self,
        *,
        spec: Dict[str, Any],
        figure_id: str,
        usage: Dict[str, Any],
        plan: Optional[Dict[str, Any]] = None,
        section: Optional[Dict[str, Any]] = None,
        spec_origin: str = "generated",
        revision: int = 0,
        previous_spec: Optional[Dict[str, Any]] = None,
        reviewer_feedback: str = "",
    ) -> Dict[str, Any]:
        if not spec:
            return {
                "status": "spec_invalid",
                "model_usage": usage,
            }
        dot_binary = _find_graphviz_dot()
        if not dot_binary:
            return {
                "status": "graphviz_unavailable",
                "model_usage": usage,
                "spec": spec,
            }
        rankdir = (
            "TB"
            if len(spec["nodes"]) >= 4
            else (
                "LR"
                if spec["layout"] == "left_to_right"
                else "TB"
            )
        )
        colors = {
            "input": "#DCEEFF",
            "mechanism": "#D8F3EE",
            "tradeoff": "#FFF0CC",
            "evidence": "#E8E1FF",
            "outcome": "#FFE0DC",
        }
        lines = [
            "digraph G {",
            f'rankdir="{rankdir}";',
            'graph [bgcolor="white", pad="0.35", nodesep="0.55", ranksep="0.7", '
            'labelloc="t", fontsize="22", fontname="Arial", dpi="150", '
            'size="10,7", ratio="compress"];',
            'node [shape="box", style="rounded,filled", color="#35566F", '
            'penwidth="1.8", fontname="Arial", fontsize="16", margin="0.18,0.12"];',
            'edge [color="#436C87", penwidth="2.0", arrowsize="0.8", '
            'fontname="Arial", fontsize="12"];',
            f'label="{html.escape(spec["title"])}";',
        ]
        for node in spec["nodes"]:
            fill = colors.get(node["kind"], "#EAF0F4")
            lines.append(
                f'{node["id"]} [label="{html.escape(node["label"])}", '
                f'fillcolor="{fill}"];'
            )
        for edge in spec["edges"]:
            label = html.escape(edge["label"])
            lines.append(
                f'{edge["source"]} -> {edge["target"]} '
                f'[label="{label}"];'
            )
        lines.append("}")
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", figure_id)
        dot_path = self.output_dir / f"{stem}.dot"
        png_path = self.output_dir / f"{stem}.png"
        dot_path.write_text("\n".join(lines), encoding="utf-8")
        completed = subprocess.run(
            [dot_binary, "-Tpng", str(dot_path), "-o", str(png_path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0 or not png_path.is_file():
            return {
                "status": "render_failed",
                "error": completed.stderr[-1000:],
                "model_usage": usage,
                "spec": spec,
            }
        with Image.open(png_path) as source:
            rendered_image = source.convert("RGB")
        fitted = ImageOps.contain(rendered_image, (1500, 960))
        # The canvas follows the diagram's height instead of being fixed at
        # 1080.  graphviz sizes its output tightly to the graph, so a wide
        # shallow diagram arrives about 1500x420 -- pasting that at y=28 on a
        # fixed 1080-tall canvas left roughly 630px of white between the last
        # node and the footer rule, i.e. well over half the delivered image
        # was blank.  FIG-GEN-001 and FIG-GEN-003 in the visA_stage2b run both
        # rendered that way and read as broken figures.
        #
        # Every measurement is preserved rather than re-tuned.  A 28px top
        # margin plus a 92px footer band reproduces the original geometry
        # exactly at the (1500, 960) content cap: 1080 tall, rule at y=1010,
        # label at y=1028.  A full-height diagram therefore renders exactly as
        # it did before and only shallow ones shrink.  The gap between the
        # diagram and the rule is a constant 22px -- what the fixed canvas
        # happened to give the tall case.
        top_margin = 28
        footer_band_height = 92
        canvas_height = fitted.height + top_margin + footer_band_height
        canvas = Image.new("RGB", (1600, canvas_height), "white")
        canvas.paste(
            fitted,
            (
                (canvas.width - fitted.width) // 2,
                top_margin,
            ),
        )
        draw = ImageDraw.Draw(canvas)
        draw.line(
            [(40, canvas_height - 70), (1560, canvas_height - 70)],
            fill="#777777",
            width=2,
        )
        draw.text(
            (650, canvas_height - 52),
            "AI-assisted explanatory diagram",
            fill="#333333",
        )
        canvas.save(png_path, format="PNG", optimize=True)
        sidecar = png_path.with_suffix(".provenance.json")
        atomic_write_json(
            sidecar,
            {
                "schema_version": "conceptual_diagram.v1",
                "rendering": "qwen_json_spec_plus_graphviz",
                "figure_id": figure_id,
                "spec": spec,
                "source_plan": plan,
                "source_label": "AI-assisted explanatory diagram",
                "spec_origin": spec_origin,
                "revision": revision,
                "previous_spec": previous_spec,
                "reviewer_feedback": reviewer_feedback,
            },
        )
        return {
            "status": "ready",
            "local_image_path": str(png_path),
            "provenance_path": str(sidecar),
            "spec": spec,
            "model_usage": usage,
            "spec_origin": spec_origin,
            "revision": revision,
        }

    def render(
        self,
        *,
        plan: Dict[str, Any],
        section: Dict[str, Any],
        figure_id: str,
    ) -> Dict[str, Any]:
        spec, usage = self._build_spec(plan=plan, section=section)
        return self._render_spec(
            spec=spec,
            figure_id=figure_id,
            usage=usage,
            plan=plan,
            section=section,
            spec_origin="generated",
        )

    def render_explicit_spec(
        self,
        *,
        spec: Dict[str, Any] | str,
        figure_id: str,
        plan: Optional[Dict[str, Any]] = None,
        section: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Render a caller-supplied spec locally after validation."""

        if isinstance(spec, str):
            spec = _safe_json(spec)
        validated = _validate_spec(dict(spec) if isinstance(spec, dict) else {})
        return self._render_spec(
            spec=validated,
            figure_id=figure_id,
            usage={},
            plan=plan,
            section=section,
            spec_origin="explicit",
        )

    def revise_spec(
        self,
        *,
        previous_spec: Dict[str, Any],
        reviewer_feedback: str,
        plan: Dict[str, Any],
        section: Dict[str, Any],
        figure_id: str,
        revision: int = 1,
    ) -> Dict[str, Any]:
        """Ask the text model to revise a prior spec from reviewer feedback.

        The previous spec and feedback are sent as compact table-like content;
        the model returns only semantic fields, and validation stays local.
        Failures are returned deterministically instead of raising.
        """

        if not isinstance(previous_spec, dict) or not previous_spec:
            return {
                "status": "revision_failed",
                "error": "previous_spec_missing",
                "model_usage": {},
            }
        feedback = str(reviewer_feedback or "").strip()
        if not feedback:
            return {
                "status": "revision_failed",
                "error": "reviewer_feedback_empty",
                "model_usage": {},
            }
        if not self.real_llm:
            return {
                "status": "revision_unavailable",
                "reason": "real_llm_disabled",
                "model_usage": {},
            }
        payload = {
            "task": "revision",
            "figure_kind": plan.get("figure_kind", ""),
            "scientific_purpose": plan.get(
                "argumentative_purpose",
                plan.get("argument_role", ""),
            ),
            "generation_brief": plan.get("generation_brief", ""),
            "section_title": section.get(
                "section_title",
                section.get("title", ""),
            ),
            "section_argument": section.get(
                "chapter_argument",
                section.get("argument_role", ""),
            ),
            "data_provenance_level": plan.get(
                "data_provenance_level",
                "schematic",
            ),
            "previous_spec": self._compact_spec_table(previous_spec),
            "reviewer_feedback": feedback,
            "revision_round": max(1, int(revision)),
            "max_semantic_revisions": MAX_SEMANTIC_REVISIONS,
        }
        try:
            response = self.llm_call(
                "ConceptualDiagramSpecGenerator",
                [
                    {
                        "role": "system",
                        "content": PROMPT_PATH.read_text(encoding="utf-8"),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                ],
                model_tier=self.model_tier,
                max_retries=1,
                temperature=0,
                max_tokens=900,
                response_format={"type": "json_object"},
                force_mock=False,
                timeout_seconds=120,
                max_transport_key_candidates=2,
                allow_model_fallback=True,
            )
        except Exception as exc:
            return {
                "status": "revision_failed",
                "error": f"{type(exc).__name__}:{exc}",
                "model_usage": {},
            }
        usage = dict(response.get("_llm_usage") or {})
        revised = _safe_json(str(response.get("content") or ""))
        revised = {
            key: revised[key]
            for key in ("title", "layout", "nodes", "edges", "takeaway")
            if key in revised
        }
        spec = _validate_spec(revised)
        if not spec:
            return {
                "status": "spec_invalid",
                "error": "revised_spec_invalid",
                "model_usage": usage,
                "revised_spec_raw": revised,
            }
        return self._render_spec(
            spec=spec,
            figure_id=figure_id,
            usage=usage,
            plan=plan,
            section=section,
            spec_origin="revision",
            revision=max(1, int(revision)),
            previous_spec=previous_spec,
            reviewer_feedback=feedback,
        )
