"""Safe AgentScope tools for article-level editorial review.

The managing editor receives a compact review map first and reads full section
text only when needed.  This bounds context growth and prevents the old
all-sections-in-every-prompt cost pattern.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentscope.tool import FunctionTool

from .artifact_store import atomic_write_json
from .review_mentor_library import (
    REVIEW_MENTOR_CATEGORIES,
    retrieve_mentor_moves,
)
from .tool_provider import ToolProvider

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_SAFE_FLAG_TYPE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_SEVERITIES = {"info", "warning", "error"}
_MAX_FLAGS = 40


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _section_text(section: Dict[str, Any], merged_draft: str) -> str:
    work_dir = Path(str(section.get("work_dir") or ""))
    draft_path = work_dir / "SECTION_DRAFT_EN.md"
    if draft_path.exists():
        return draft_path.read_text(encoding="utf-8", errors="replace").strip()

    title = str(section.get("title") or section.get("section_id") or "")
    if title:
        pattern = rf"(?ms)^##\s+{re.escape(title)}\s*$\n(.*?)(?=^##\s+|\Z)"
        match = re.search(pattern, merged_draft)
        if match:
            return match.group(1).strip()
    return ""


def _normalize_flag_type(raw: Any) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", str(raw or "").strip().lower())
    value = value.strip("_")
    return value[:64] if value else "editorial_quality"


def _normalize_severity(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    aliases = {
        "critical": "error",
        "major": "error",
        "high": "error",
        "moderate": "warning",
        "medium": "warning",
        "minor": "warning",
        "low": "info",
    }
    return aliases.get(value, value if value in _SEVERITIES else "warning")


class GlobalAuditToolProvider(ToolProvider):
    """Expose bounded read, submit, and deterministic validation tools."""

    TOOL_NAMES = [
        "load_global_review_context",
        "consult_review_mentor_for_audit",
        "read_section_text",
        "submit_audit_flags",
        "validate_global_audit_package",
    ]

    def __init__(
        self,
        merged_draft: str,
        section_registry: Dict[str, Any],
        blueprint: Dict[str, Any],
        work_dir: Path,
        round_num: int,
        m1_library_path: Optional[Path] = None,
        max_section_reads: int = 2,
    ) -> None:
        self.merged_draft = merged_draft
        self.section_registry = section_registry
        self.blueprint = blueprint
        self.work_dir = work_dir
        self.round_num = round_num
        self.m1_library_path = m1_library_path
        self.max_section_reads = max(0, int(max_section_reads))
        self._section_reads_used = 0
        self.audit_dir = work_dir / f"audit_round_{round_num}"
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.output_path = self.audit_dir / "LAYER2_AUDIT_FLAGS.json"
        self._sections = {
            str(section.get("section_id")): section
            for section in section_registry.get("sections", [])
            if section.get("section_id")
        }

    def get_allowed_tool_names(self) -> List[str]:
        names = list(self.TOOL_NAMES)
        if not (
            self.m1_library_path
            and self.m1_library_path.exists()
        ):
            names.remove("consult_review_mentor_for_audit")
        return names

    def get_tools(self, work_dir: Path) -> list:
        provider = self

        def load_global_review_context() -> str:
            """Load the user question, article thesis, and compact section map."""

            input_context = provider.blueprint.get("input_context", {})
            section_cards = []
            for section_id, section in provider._sections.items():
                text = _section_text(section, provider.merged_draft)
                paragraphs = [
                    paragraph.strip()
                    for paragraph in re.split(r"\n\s*\n", text)
                    if paragraph.strip()
                ]
                section_work_dir = Path(str(section.get("work_dir") or ""))
                visual = _load_json(
                    section_work_dir / "SECTION_VISUAL_PLACEMENT.json"
                )
                citation = _load_json(
                    section_work_dir / "SECTION_CITATION_AUDIT.json"
                )
                refs = re.findall(r"\[REF:([^\]]+)\]", text)
                ref_counts: Dict[str, int] = {}
                for ref in refs:
                    ref_counts[ref] = ref_counts.get(ref, 0) + 1
                citation_rows = citation.get("citations", [])
                if not isinstance(citation_rows, list):
                    citation_rows = []
                # A stale or legacy section audit can report zero even after a
                # later revision inserted valid reference markers.  The
                # managing editor must see the observable article state, not
                # trust a stale counter and invent a system-wide citation
                # failure.  Keep all three signals and use the strongest.
                citation_count = max(
                    int(citation.get("total_citations") or 0),
                    len(citation_rows),
                    len(ref_counts),
                )
                paragraph_map = [
                    {
                        "paragraph": index + 1,
                        "preview": paragraph[:360],
                    }
                    for index, paragraph in enumerate(paragraphs[:14])
                ]
                section_cards.append(
                    {
                        "section_id": section_id,
                        "title": section.get("title", ""),
                        "argument_role": section.get("argument_role", ""),
                        "chapter_argument": section.get("chapter_argument", ""),
                        "status": section.get("status", ""),
                        "word_count": len(text.split()),
                        "opening_preview": (
                            paragraphs[0][:700] if paragraphs else ""
                        ),
                        "closing_preview": (
                            paragraphs[-1][:700] if paragraphs else ""
                        ),
                        "paragraph_map": paragraph_map,
                        "unique_cited_papers": len(ref_counts),
                        "largest_source_marker_share": round(
                            max(ref_counts.values()) / max(1, len(refs)), 3
                        )
                        if ref_counts
                        else 0.0,
                        "top_cited_papers": [
                            {
                                "paper_id": paper_id,
                                "marker_count": count,
                            }
                            for paper_id, count in sorted(
                                ref_counts.items(),
                                key=lambda item: item[1],
                                reverse=True,
                            )[:4]
                        ],
                        "visual_placement_count": len(
                            visual.get("placements", [])
                        ),
                        "citation_count": citation_count,
                        "citation_count_sources": {
                            "audit_total": int(
                                citation.get("total_citations") or 0
                            ),
                            "audit_rows": len(citation_rows),
                            "inline_unique_markers": len(ref_counts),
                        },
                    }
                )
            payload = {
                "status": "ok",
                "round": provider.round_num,
                "user_question": input_context.get("user_question", ""),
                "problem_understanding": input_context.get(
                    "problem_understanding", ""
                ),
                "scope_definition": input_context.get("scope_definition", ""),
                "review_thesis": provider.blueprint.get(
                    "full_review_argument",
                    provider.blueprint.get("review_thesis", ""),
                ),
                "methodology_identity": provider.blueprint.get(
                    "methodology_identity", ""
                ),
                "section_count": len(section_cards),
                "sections": section_cards,
                "review_mentor_available": bool(
                    provider.m1_library_path
                    and provider.m1_library_path.exists()
                ),
                "inspection_budget": {
                    "maximum_section_text_calls": provider.max_section_reads,
                    "instruction": (
                        "Use the paragraph map as the primary evidence. Inspect "
                        "only the one or two sections needed to verify a material "
                        "flag, then submit the smallest sufficient flag set."
                    ),
                },
            }
            return json.dumps(payload, ensure_ascii=True)

        def consult_review_mentor_for_audit(
            categories_json: str,
            editorial_question: str,
            max_per_category: int = 2,
        ) -> str:
            """Retrieve transferable top-review moves for an editorial problem."""

            if (
                provider.m1_library_path is None
                or not provider.m1_library_path.exists()
            ):
                return json.dumps(
                    {"status": "error", "error": "mentor_library_unavailable"},
                    ensure_ascii=True,
                )
            try:
                requested = json.loads(categories_json)
            except Exception:
                requested = []
            if not isinstance(requested, list):
                requested = []
            categories = [
                str(item)
                for item in requested
                if str(item) in REVIEW_MENTOR_CATEGORIES
            ]
            if not categories:
                categories = [
                    "section_progression",
                    "synthesis_moves",
                    "evidence_critique",
                    "top_journal_publishability",
                ]
            moves = retrieve_mentor_moves(
                provider.m1_library_path,
                categories=categories,
                planning_question=str(editorial_question),
                max_per_category=max_per_category,
            )
            return json.dumps(
                {
                    "status": "ok",
                    "editorial_question": str(editorial_question)[:1000],
                    "usage_rule": (
                        "Use these records only as abstract editorial heuristics. "
                        "They are not scientific evidence and must never be cited."
                    ),
                    "mentor_moves": moves,
                },
                ensure_ascii=True,
            )

        def read_section_text(
            section_id: str,
            start_paragraph: int = 0,
            max_paragraphs: int = 12,
        ) -> str:
            """Read a bounded paragraph window from one section draft."""

            if provider._section_reads_used >= provider.max_section_reads:
                return json.dumps(
                    {
                        "status": "error",
                        "error": "section_read_budget_exhausted",
                        "instruction": (
                            "Do not inspect more sections. Submit the material "
                            "audit flags supported by the compact paragraph map "
                            "and the section windows already read."
                        ),
                    },
                    ensure_ascii=True,
                )
            section = provider._sections.get(str(section_id))
            if section is None:
                return json.dumps(
                    {"status": "error", "error": "unknown_section_id"},
                    ensure_ascii=True,
                )
            text = _section_text(section, provider.merged_draft)
            paragraphs = [
                paragraph.strip()
                for paragraph in re.split(r"\n\s*\n", text)
                if paragraph.strip()
            ]
            start = max(0, int(start_paragraph))
            count = min(max(1, int(max_paragraphs)), 20)
            selected = paragraphs[start : start + count]
            provider._section_reads_used += 1
            return json.dumps(
                {
                    "status": "ok",
                    "section_id": section_id,
                    "start_paragraph": start,
                    "returned_paragraphs": len(selected),
                    "total_paragraphs": len(paragraphs),
                    "has_more": start + count < len(paragraphs),
                    "section_reads_remaining": max(
                        0,
                        provider.max_section_reads
                        - provider._section_reads_used,
                    ),
                    "text": "\n\n".join(selected),
                },
                ensure_ascii=True,
            )

        def submit_audit_flags(flags_json: str) -> str:
            """Submit a JSON array of material article-level audit flags."""

            try:
                parsed = json.loads(flags_json)
            except Exception as exc:
                return json.dumps(
                    {"status": "error", "error": f"invalid_json: {exc}"},
                    ensure_ascii=True,
                )
            flags = parsed.get("flags", []) if isinstance(parsed, dict) else parsed
            if not isinstance(flags, list):
                return json.dumps(
                    {"status": "error", "error": "flags must be a JSON list"},
                    ensure_ascii=True,
                )
            if len(flags) > _MAX_FLAGS:
                return json.dumps(
                    {
                        "status": "error",
                        "error": f"too_many_flags: maximum is {_MAX_FLAGS}",
                    },
                    ensure_ascii=True,
                )

            normalized = []
            errors = []
            known_sections = set(provider._sections)
            for index, raw in enumerate(flags):
                if not isinstance(raw, dict):
                    errors.append(f"flag[{index}] is not an object")
                    continue
                flag_type = _normalize_flag_type(
                    raw.get("type") or raw.get("category")
                )
                severity = _normalize_severity(raw.get("severity"))
                section_ids = (
                    raw.get("section_ids")
                    or raw.get("location")
                    or []
                )
                if isinstance(section_ids, str):
                    section_ids = re.findall(
                        r"S\d{2}",
                        section_ids.upper(),
                    )
                description = str(raw.get("description") or "").strip()
                blocking = raw.get("blocking")
                if not _SAFE_FLAG_TYPE.fullmatch(flag_type):
                    errors.append(f"flag[{index}] has invalid type")
                if severity not in _SEVERITIES:
                    errors.append(f"flag[{index}] has invalid severity")
                if not isinstance(section_ids, list) or any(
                    section_id not in known_sections
                    for section_id in section_ids
                ):
                    errors.append(f"flag[{index}] has unknown section_ids")
                if len(description) < 20 or _CJK.search(description):
                    errors.append(
                        f"flag[{index}] description must be substantive English"
                    )
                if not isinstance(blocking, bool):
                    blocking = severity == "error"
                normalized.append(
                    {
                        "type": flag_type,
                        "severity": severity,
                        "section_ids": section_ids,
                        "description": description,
                        "blocking": bool(blocking),
                        "root_cause": str(
                            raw.get("root_cause")
                            or raw.get("rationale")
                            or ""
                        ).strip(),
                        "recommended_action": str(
                            raw.get("recommended_action")
                            or raw.get("recommendation")
                            or ""
                        ).strip(),
                    }
                )
            if errors:
                return json.dumps(
                    {"status": "error", "errors": errors[:20]},
                    ensure_ascii=True,
                )
            atomic_write_json(
                provider.output_path,
                {
                    "schema_version": "phase4.layer2_audit.v2",
                    "round": provider.round_num,
                    "flags": normalized,
                },
            )
            return json.dumps(
                {
                    "status": "ok",
                    "artifact": provider.output_path.name,
                    "flag_count": len(normalized),
                },
                ensure_ascii=True,
            )

        def validate_global_audit_package() -> str:
            """Validate the audit artifact independently of the model."""

            if not provider.output_path.exists():
                return "VALIDATION_FAILED: LAYER2_AUDIT_FLAGS.json is missing."
            raw = _load_json(provider.output_path)
            flags = raw.get("flags")
            if not isinstance(flags, list):
                return "VALIDATION_FAILED: flags must be a list."
            if len(flags) > _MAX_FLAGS:
                return "VALIDATION_FAILED: too many flags."
            known_sections = set(provider._sections)
            for index, flag in enumerate(flags):
                if (
                    not isinstance(flag, dict)
                    or not _SAFE_FLAG_TYPE.fullmatch(str(flag.get("type") or ""))
                    or flag.get("severity") not in _SEVERITIES
                    or not isinstance(flag.get("blocking"), bool)
                    or any(
                        section_id not in known_sections
                        for section_id in flag.get("section_ids", [])
                    )
                ):
                    return f"VALIDATION_FAILED: malformed flag at index {index}."
            return (
                "VALIDATION_PASSED: article-level audit package is structurally "
                f"valid with {len(flags)} material flags."
            )

        return [
            FunctionTool(load_global_review_context),
            FunctionTool(consult_review_mentor_for_audit),
            FunctionTool(read_section_text),
            FunctionTool(submit_audit_flags),
            FunctionTool(validate_global_audit_package),
        ]
