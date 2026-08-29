"""Generate disclosed explanatory scientific visuals without fake citations.

Only visual gaps explicitly classified as author-synthesized explanatory
visuals are eligible.  Exact, approximate, and schematic input data are kept
distinct.  Every output is saved locally, labelled as AI-generated explanatory
material, and checked by a vision model before the configured human/default
review decision.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from config.qwen_config import get_qwen_api_key_candidates
from llm.qwen_vision_client import call_qwen_vision


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PROMPT = PROJECT_ROOT / "prompts" / "Conceptual Scientific Figure Generator.txt"
AUDITOR_PROMPT = PROJECT_ROOT / "prompts" / "Generated Conceptual Figure Auditor.txt"


def _safe_json(text: str) -> dict[str, Any]:
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


def _response_image_url(response: Any) -> str:
    try:
        output = response.output if hasattr(response, "output") else response["output"]
        choices = output.choices if hasattr(output, "choices") else output["choices"]
        message = choices[0].message if hasattr(choices[0], "message") else choices[0]["message"]
        content = message.content if hasattr(message, "content") else message["content"]
        for item in content:
            value = item.get("image") if isinstance(item, dict) else getattr(item, "image", "")
            if value:
                return str(value)
    except Exception:
        return ""
    return ""


def _build_image_prompt(task: dict[str, Any]) -> str:
    """Build an image brief, not a block of instructions to typeset."""

    provenance = str(task.get("data_provenance_level") or "schematic")
    data = task.get("input_data") or {}
    data_note = (
        "Use only the supplied data relationships: "
        + json.dumps(data, ensure_ascii=False)
        if data
        else (
            "Show qualitative relationships only. Do not invent numerical "
            "values, measured-looking spectra, axes ticks, or citations."
        )
    )
    return "\n".join(
        [
            "Create one polished scientific review figure. Return the figure only.",
            "Do NOT typeset this prompt, field names, JSON, rules, bullet lists, "
            "or meta-instructions inside the image.",
            "Use a clean white background, restrained blue/teal/orange palette, "
            "clear arrows, generous spacing, and no decorative photography.",
            "Keep visible text minimal: short scientific labels only. Every label "
            "must be correctly spelled and legible.",
            f"Figure form: {task.get('figure_kind') or 'mechanism schematic'}.",
            f"Scientific purpose: {task.get('generation_brief') or task.get('argument_role') or ''}",
            f"Data identity: {provenance}. {data_note}",
            "Do not include a paper title, author, DOI, fake citation, prompt "
            "heading, or software interface.",
            "Leave a narrow blank footer; the disclosure will be stamped "
            "deterministically after generation.",
        ]
    )


def _stamp_disclosure(path: Path) -> None:
    """Add a reliable disclosure footer without trusting generated text."""

    with Image.open(path) as source:
        image = source.convert("RGB")
    footer_height = max(46, image.height // 24)
    canvas = Image.new(
        "RGB",
        (image.width, image.height + footer_height),
        "white",
    )
    canvas.paste(image, (0, 0))
    draw = ImageDraw.Draw(canvas)
    label = "AI-generated explanatory visual"
    draw.line(
        [(24, image.height + 4), (image.width - 24, image.height + 4)],
        fill="#777777",
        width=2,
    )
    draw.text(
        (max(24, image.width // 2 - 120), image.height + 14),
        label,
        fill="#333333",
    )
    canvas.save(path, format="PNG", optimize=True)


@dataclass
class ConceptualVisualGenerator:
    output_dir: Path
    model: str = "qwen-image-2.0"
    size: str = "2048*2048"
    vision_model_tier: str = "vision_premium_model"
    real_llm: bool = False
    max_transport_attempts: int = 2
    generation_timeout_seconds: int = 75
    _generator_prompt: str = field(init=False, default="")
    _auditor_prompt: str = field(init=False, default="")

    def _model_candidates(self) -> list[str]:
        """Return a bounded quality-first fallback chain.

        Qwen-Image 3.0 is a limited-preview model, so it is attempted only
        when explicitly configured.  Stable 2.0 models remain automatic
        fallbacks and one optional figure never fans out across an unbounded
        model/key matrix.
        """

        configured = str(self.model or "qwen-image-2.0-pro").strip()
        candidates = [configured]
        if configured == "qwen-image-3.0-pro":
            candidates.extend(["qwen-image-2.0-pro", "qwen-image-2.0"])
        elif configured != "qwen-image-2.0":
            candidates.append("qwen-image-2.0")
        return list(dict.fromkeys(candidates))

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._generator_prompt = GENERATOR_PROMPT.read_text(encoding="utf-8")
        self._auditor_prompt = AUDITOR_PROMPT.read_text(encoding="utf-8")

    def generate(
        self,
        *,
        plan: dict[str, Any],
        section: dict[str, Any],
    ) -> dict[str, Any]:
        result = dict(plan)
        if str(plan.get("creation_class") or "") != "author_synthesized_conceptual_schematic":
            result["generation_status"] = "not_eligible_empirical_or_source_based_visual"
            return result
        if not self.real_llm:
            result["generation_status"] = "mock_not_generated"
            return result

        plan_id = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "_",
            str(plan.get("visual_plan_id") or "visual"),
        )
        cache_payload = {
            "plan": plan,
            "section": {
                "section_id": section.get("section_id", ""),
                "title": section.get(
                    "section_title",
                    section.get("title", ""),
                ),
                "argument": section.get(
                    "chapter_argument",
                    section.get("argument_role", ""),
                ),
            },
            "requested_model": self.model,
            "size": self.size,
            "generator_prompt_sha256": hashlib.sha256(
                self._generator_prompt.encode("utf-8")
            ).hexdigest(),
            "auditor_prompt_sha256": hashlib.sha256(
                self._auditor_prompt.encode("utf-8")
            ).hexdigest(),
        }
        generation_fingerprint = hashlib.sha256(
            json.dumps(
                cache_payload,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        cache_id = f"{plan_id}_{generation_fingerprint[:16]}"
        image_path = self.output_dir / f"{cache_id}.png"
        sidecar = image_path.with_suffix(".provenance.json")
        # Generation URLs are ephemeral and image calls are expensive. Reuse a
        # fully materialized asset only when its provenance sidecar can also be
        # read; a bare image without provenance is never silently trusted.
        if image_path.exists() and sidecar.exists():
            try:
                provenance = json.loads(sidecar.read_text(encoding="utf-8"))
                parsed_audit = provenance.get("vision_audit") or {}
                vision_used = bool(provenance.get("vision_used"))
                approved = bool(
                    vision_used
                    and str(parsed_audit.get("verdict") or "").lower() == "approve"
                )
                result.update({
                    "generation_status": (
                        "model_approved_human_pending"
                        if approved else "model_rejected_or_revision_required"
                    ),
                    "generation_cache_hit": True,
                    "local_image_path": str(image_path),
                    "provenance_path": str(sidecar),
                    "source_label": "AI-generated conceptual schematic",
                    "evidence_status": "explanatory_not_empirical_evidence",
                    "model_review": parsed_audit,
                    "model_review_vision_used": vision_used,
                    "needs_human_review": True,
                })
                return result
            except Exception:
                pass

        claims = [
            {
                "claim_id": row.get("claim_id"),
                "authorized_statement": row.get("statement_for_writing") or row.get("statement"),
                "writing_permission": row.get("writing_permission"),
            }
            for row in (section.get("claims") or [])
            if isinstance(row, dict)
        ][:8]
        task = {
            "visual_plan_id": plan.get("visual_plan_id"),
            "section_title": section.get("section_title") or section.get("title"),
            "argument_role": plan.get("argument_role"),
            "figure_kind": plan.get("figure_kind") or "mechanism_schematic",
            "generation_brief": plan.get("generation_brief") or "",
            "data_provenance_level": (
                plan.get("data_provenance_level") or "schematic"
            ),
            "input_data": plan.get("input_data") or {},
            "authorized_claim_context": claims,
            "prohibited_use": plan.get("prohibited_use"),
            "rendering_constraint": (
                "Use only supplied exact/approximate data. If no input data are "
                "supplied, render qualitative relationships without invented precision."
            ),
        }
        image_prompt = (
            self._generator_prompt.strip()
            + "\n\n"
            + _build_image_prompt(task)
        )
        keys = get_qwen_api_key_candidates()
        last_error = "no_qwen_key"
        url = ""
        used_key_source = ""
        used_model = ""
        generation_attempts: list[dict[str, str]] = []
        # A transport/model failure must not rotate through the Cartesian
        # product of every model and key.  The generated figure is optional
        # explanatory material and the factory has a deterministic structured
        # fallback, so the entire generation path receives one global attempt
        # budget.
        attempts_used = 0
        for model_name in self._model_candidates():
            if url or attempts_used >= max(1, self.max_transport_attempts):
                break
            for key in keys[:2]:
                if attempts_used >= max(1, self.max_transport_attempts):
                    break
                attempts_used += 1
                try:
                    from dashscope import MultiModalConversation

                    response = MultiModalConversation.call(
                        api_key=key["api_key"],
                        model=model_name,
                        messages=[{"role": "user", "content": [{"text": image_prompt}]}],
                        result_format="message",
                        stream=False,
                        watermark=False,
                        prompt_extend=False,
                        negative_prompt=(
                            "decorative art, fabricated data, unsupported numerical precision, "
                            "fake spectrum, fake measurement, fake citation, illegible dense text, watermark"
                        ),
                        size=self.size,
                        timeout=max(
                            15,
                            int(self.generation_timeout_seconds),
                        ),
                    )
                    status_code = int(getattr(response, "status_code", 0) or 0)
                    if status_code and status_code != 200:
                        last_error = (
                            f"HTTP_{status_code}:{getattr(response, 'code', '')}"
                        )
                        generation_attempts.append(
                            {"model": model_name, "result": last_error}
                        )
                        continue
                    url = _response_image_url(response)
                    if not url:
                        last_error = "response_missing_image_url"
                        generation_attempts.append(
                            {"model": model_name, "result": last_error}
                        )
                        continue
                    used_key_source = str(key.get("api_key_source") or "")
                    used_model = model_name
                    generation_attempts.append(
                        {"model": model_name, "result": "success"}
                    )
                    break
                except Exception as exc:
                    last_error = f"{type(exc).__name__}:{exc}"
                    generation_attempts.append(
                        {"model": model_name, "result": last_error}
                    )
        if not url:
            result.update({
                "generation_status": "generation_failed",
                "generation_error": last_error,
                "generation_attempts": generation_attempts,
                "evidence_status": "not_evidence_generation_failed",
            })
            return result

        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                image_path.write_bytes(response.read())
            _stamp_disclosure(image_path)
        except Exception as exc:
            result.update({
                "generation_status": "download_failed",
                "generation_error": f"{type(exc).__name__}:{exc}",
                "evidence_status": "not_evidence_download_failed",
            })
            return result

        audit_payload = {
            "argument_role": plan.get("argument_role"),
            "section_title": section.get("section_title") or section.get("title"),
            "figure_kind": plan.get("figure_kind") or "mechanism_schematic",
            "generation_brief": plan.get("generation_brief") or "",
            "data_provenance_level": (
                plan.get("data_provenance_level") or "schematic"
            ),
            "input_data": plan.get("input_data") or {},
            "authorized_claim_context": claims,
            "required_label": "AI-generated explanatory visual",
        }
        try:
            audit = call_qwen_vision(
                "GeneratedConceptualFigureAuditor",
                self._auditor_prompt + "\n\nTASK JSON:\n" + json.dumps(audit_payload, ensure_ascii=False),
                local_image_path=image_path,
                model_tier=self.vision_model_tier,
                max_retries=0,
                temperature=0,
                max_tokens=1200,
                response_format={"type": "json_object"},
                force_mock=False,
                timeout_seconds=150,
                max_transport_key_candidates=1,
                allow_model_fallback=True,
            )
        except Exception as exc:
            audit = {
                "content": json.dumps(
                    {
                        "verdict": "needs_human_review",
                        "misleading_elements": [
                            f"Vision audit unavailable: {type(exc).__name__}"
                        ],
                    }
                ),
                "_vision_used": False,
                "_llm_usage": {},
            }
        parsed_audit = _safe_json(str(audit.get("content") or ""))
        vision_used = bool(audit.get("_vision_used"))
        verdict = str(parsed_audit.get("verdict") or "reject").lower()
        numeric_labels = [
            str(value) for value in (parsed_audit.get("numeric_labels") or []) if str(value)
        ]
        unsupported_numeric_labels = [
            str(value)
            for value in (
                parsed_audit.get("unsupported_numeric_labels") or []
            )
            if str(value)
        ]
        input_data = plan.get("input_data") or {}
        has_authorized_data = bool(input_data)
        trend_correct = parsed_audit.get("trend_direction_correct")
        fabricated = bool(
            parsed_audit.get("contains_fabricated_empirical_content")
        )
        approved = bool(
            vision_used
            and verdict == "approve"
            and not fabricated
            and trend_correct is not False
            and not unsupported_numeric_labels
            and (has_authorized_data or not numeric_labels)
        )
        if numeric_labels and not has_authorized_data:
            parsed_audit["verdict"] = "reject"
            parsed_audit["contains_fabricated_empirical_content"] = True
            parsed_audit.setdefault("misleading_elements", []).append(
                "Numeric labels were shown without supplied input data: "
                + ", ".join(numeric_labels[:12])
            )
        provenance = {
            "schema_version": "generated_conceptual_visual.v1",
            "visual_plan_id": plan.get("visual_plan_id"),
            "generation_fingerprint": generation_fingerprint,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "model": used_model or self.model,
            "requested_model": self.model,
            "generation_attempts": generation_attempts,
            "prompt": image_prompt,
            "api_key_source": used_key_source,
            "local_image_path": str(image_path),
            "source_label": "AI-generated explanatory visual",
            "evidence_class": "explanatory_visual_not_paper_original",
            "data_provenance_level": (
                plan.get("data_provenance_level") or "schematic"
            ),
            "input_data": input_data,
            "vision_audit": parsed_audit,
            "vision_used": vision_used,
            "human_approval_required": True,
        }
        sidecar.write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
        result.update({
            "generation_status": (
                "model_approved_human_pending" if approved else "model_rejected_or_revision_required"
            ),
            "generation_model_used": used_model or self.model,
            "generation_attempts": generation_attempts,
            "local_image_path": str(image_path),
            "provenance_path": str(sidecar),
            "source_label": "AI-generated explanatory visual",
            "evidence_status": "explanatory_visual_not_paper_original",
            "model_review": parsed_audit,
            "model_review_vision_used": vision_used,
            "model_review_usage": dict(audit.get("_llm_usage") or {}),
            "needs_human_review": True,
        })
        return result


def generate_conceptual_visual_gaps(
    *,
    visual_gap_plan: list[dict[str, Any]],
    blueprint: dict[str, Any],
    output_dir: Path,
    real_llm: bool,
    max_assets: int = 4,
) -> list[dict[str, Any]]:
    by_section = {
        str(row.get("section_id") or ""): row
        for row in (blueprint.get("sections") or [])
        if isinstance(row, dict)
    }
    generator = ConceptualVisualGenerator(output_dir=output_dir, real_llm=real_llm)
    def priority(plan: dict[str, Any]) -> tuple[int, int, str]:
        section = by_section.get(str(plan.get("section_id") or ""), {})
        role = str(section.get("section_role") or "").lower()
        argument = str(plan.get("argument_role") or "").lower()
        role_rank = 0 if role in {"introduction", "synthesis", "conclusion", "outlook"} else 1
        argument_rank = 0 if "framework" in argument else 1 if "conceptual" in argument else 2
        return role_rank, argument_rank, str(plan.get("visual_plan_id") or "")

    eligible_plans = [
        plan for plan in visual_gap_plan
        if str(plan.get("creation_class") or "") == "author_synthesized_conceptual_schematic"
    ]
    selected_ids = {
        str(plan.get("visual_plan_id") or "")
        for plan in sorted(eligible_plans, key=priority)[:max(0, int(max_assets))]
    }
    results: list[dict[str, Any]] = []
    for plan in visual_gap_plan:
        eligible = str(plan.get("creation_class") or "") == "author_synthesized_conceptual_schematic"
        if eligible and str(plan.get("visual_plan_id") or "") in selected_ids:
            row = generator.generate(
                plan=plan,
                section=by_section.get(str(plan.get("section_id") or ""), {}),
            )
        else:
            row = dict(plan)
            row.setdefault(
                "generation_status",
                "not_generated_empirical_or_budget_limited",
            )
        results.append(row)
    return results
