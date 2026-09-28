"""AgentScope tools for the Phase-3 Review Lead.

The Review Lead designs the intellectual architecture.  It sees a compact
knowledge-base overview and selected M1 writing moves, but it does not mount
scientific evidence or write prose.  Section researchers perform that work
after the blueprint is accepted.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentscope.tool import FunctionTool
from pydantic import ValidationError

from .article_completion_schemas import ArticleRhetoricalContract
from .artifact_store import atomic_write_json
from .review_mentor_library import (
    REVIEW_MENTOR_CATEGORIES,
    retrieve_mentor_moves,
)
from .topic_identity import assess_topic_alignment
from .tool_provider import ToolProvider
from .review_quality_contract import resolve_review_contract
from .review_scope_map import attach_review_scope_map

COVERAGE_ROLES = (
    "foundation",
    "mechanism",
    "method",
    "frontier",
    "controversy",
    "application",
)
METHODOLOGY_IDENTITIES = {
    "critical_narrative_review",
    "scoping_review",
    "systematic_review",
    "perspective_review",
}
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9-]{2,}")


def _legacy_rhetorical_contract(
    blueprint: Dict[str, Any],
    ctx: "ReviewLeadContext",
) -> Dict[str, Any]:
    """Build a safe compatibility contract for pre-upgrade blueprints.

    This preserves resumability for historical runs.  It does not invent
    scientific claims: every value is either copied from the confirmed brief
    and blueprint or is a generic writing instruction.
    """

    thesis = str(blueprint.get("review_thesis") or "").strip()
    argument = str(blueprint.get("full_review_argument") or "").strip()
    taxonomy = str(blueprint.get("taxonomy_principle") or "").strip()
    return {
        "schema_version": "article_rhetorical_contract.v1",
        "methodology_identity": str(
            blueprint.get("methodology_identity")
            or "critical_narrative_review"
        ),
        "target_article_type": "comprehensive_review",
        "target_audience": [
            "optics researchers",
            "graduate students",
            "adjacent-domain specialists",
        ],
        "provisional_title": str(
            blueprint.get("provisional_title")
            or ctx.user_question
            or "Scientific literature review"
        ),
        "central_question": ctx.user_question,
        "review_thesis": thesis,
        "distinctive_angle": argument,
        "scope_inclusions": [ctx.scope_definition],
        "scope_exclusions": [
            "Topics outside the confirmed problem definition and scope."
        ],
        "introduction_contract": {
            "why_now": (
                "Explain why the confirmed scientific question now warrants "
                "a renewed synthesis."
            ),
            "reader_prerequisites": [
                "Define only the concepts needed to follow the review argument."
            ],
            "problem_reframing": thesis,
            "review_gap": (
                "Identify the interpretive or organizational gap addressed by "
                "this review without claiming an unsupported systematic novelty."
            ),
            "scope_and_method_disclosure": (
                "State the confirmed scope and the declared review methodology."
            ),
            "roadmap_function": argument,
        },
        "body_contract": {
            "primary_taxonomy": taxonomy,
            "cross_cutting_dimensions": [
                "mechanism",
                "method",
                "evidence quality",
                "application boundary",
            ],
            "progression_logic": argument,
            "must_resolve": [
                str(section.get("chapter_argument") or "")
                for section in blueprint.get("sections", [])
                if isinstance(section, dict)
                and str(section.get("chapter_argument") or "").strip()
            ],
        },
        "outlook_contract": {
            "challenge_axes": [
                "physical_limit",
                "material_or_device_constraint",
                "measurement_or_benchmark_gap",
                "manufacturing_or_integration",
                "application_or_translation",
            ],
            "required_reasoning": [
                "state_of_evidence",
                "root_cause",
                "current_response",
                "remaining_boundary",
                "actionable_next_step",
            ],
            "speculation_policy": (
                "Separate established, conditional, and speculative statements."
            ),
        },
        "conclusion_contract": {
            "required_takeaways": 4,
            "must_answer_central_question": True,
            "no_new_topics": True,
            "no_new_evidence": True,
        },
        "abstract_contract": {
            "target_word_range": {"min": 180, "max": 300},
            "required_moves": [
                "context",
                "need",
                "scope",
                "organizing_logic",
                "major_synthesis",
                "outlook",
            ],
        },
        "global_figure_contract": {
            "candidate_templates": [
                "field_map",
                "timeline",
                "benchmark_landscape",
                "challenge_roadmap",
            ],
            "selection_rule": (
                "Use only figures with material explanatory or argumentative value."
            ),
        },
        "provenance": "legacy_compatibility",
    }


@dataclass
class ReviewLeadContext:
    user_question: str
    problem_understanding: str
    scope_definition: str
    work_dir: Path
    kb_sqlite: Optional[Path] = None
    query_plan_path: Optional[Path] = None
    m1_library_path: Optional[Path] = None
    visual_policy: Dict[str, Any] = field(default_factory=dict)
    topic_identity: Dict[str, Any] = field(default_factory=dict)


def _read_json(path: Optional[Path], default: Any) -> Any:
    if path is None or not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [
            str(row[1])
            for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        ]
    except Exception:
        return []


def _kb_overview(
    kb_path: Optional[Path],
    topic_identity: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if kb_path is None or not kb_path.exists():
        return {"available": False, "reason": "knowledge_base_not_found"}
    result: Dict[str, Any] = {
        "available": True,
        "path_name": kb_path.name,
        "counts": {},
        "top_concepts": [],
        "year_range": {},
    }
    conn = sqlite3.connect(str(kb_path))
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table in (
            "papers",
            "paper_cards",
            "text_chunks",
            "visual_assets",
            "visual_chunks",
            "concepts",
        ):
            if table in tables:
                result["counts"][table] = int(
                    conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                )

        if "concepts" in tables:
            columns = _table_columns(conn, "concepts")
            label_col = next(
                (
                    name
                    for name in ("label", "concept_label", "name", "concept")
                    if name in columns
                ),
                None,
            )
            if label_col:
                rows = conn.execute(
                    f'SELECT "{label_col}" FROM "concepts" '
                    f'WHERE "{label_col}" IS NOT NULL LIMIT 5000'
                ).fetchall()
                labels = [
                    str(row[0]).strip()
                    for row in rows
                    if str(row[0]).strip()
                ]
                contract = topic_identity or {}
                if contract.get("valid"):
                    scored = []
                    for label in labels:
                        alignment = assess_topic_alignment(
                            label,
                            contract,
                            strict=False,
                        )
                        if alignment["status"] != "passed":
                            continue
                        scored.append(
                            (
                                len(alignment["core_hits"]),
                                len(alignment["supporting_hits"]),
                                label,
                            )
                        )
                    scored.sort(reverse=True)
                    result["top_concepts"] = [
                        item[2] for item in scored[:30]
                    ]
                    result["topic_filter_status"] = (
                        "matched"
                        if scored
                        else "no_topic_matched_concepts"
                    )
                else:
                    result["top_concepts"] = []
                    result["topic_filter_status"] = (
                        "topic_identity_unavailable"
                    )

        if "papers" in tables:
            columns = _table_columns(conn, "papers")
            year_col = next(
                (
                    name
                    for name in ("year", "publication_year", "published_year")
                    if name in columns
                ),
                None,
            )
            if year_col:
                row = conn.execute(
                    f'SELECT MIN("{year_col}"), MAX("{year_col}") '
                    f'FROM "papers" WHERE "{year_col}" IS NOT NULL'
                ).fetchone()
                result["year_range"] = {"min": row[0], "max": row[1]}
        if "s2_literature_graph_nodes" in tables:
            result["s2_literature_graph"] = {
                "node_count": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM s2_literature_graph_nodes"
                    ).fetchone()[0]
                ),
                "active_node_count": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM s2_literature_graph_nodes "
                        "WHERE active_for_lineage=1"
                    ).fetchone()[0]
                ),
                "edge_count": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM s2_literature_graph_edges"
                    ).fetchone()[0]
                )
                if "s2_literature_graph_edges" in tables
                else 0,
                "usage_rule": (
                    "Use citation edges to plan historical development; use "
                    "semantic recommendations only to discover adjacent branches."
                ),
            }
    finally:
        conn.close()
    return result


class ReviewLeadToolProvider(ToolProvider):
    TOOL_NAMES = [
        "load_review_brief",
        "inspect_review_knowledge_base",
        "consult_review_mentor",
        "submit_review_blueprint",
        "validate_review_blueprint_package",
    ]

    def __init__(self, ctx: ReviewLeadContext) -> None:
        self.ctx = ctx
        self.ctx.work_dir.mkdir(parents=True, exist_ok=True)
        self.blueprint_path = self.ctx.work_dir / "REVIEW_BLUEPRINT.json"
        self.validation_path = (
            self.ctx.work_dir / "REVIEW_BLUEPRINT_VALIDATION.json"
        )
        self.mentor_trace_path = (
            self.ctx.work_dir / "REVIEW_MENTOR_TRACE.json"
        )
        self.scope_map_path = self.ctx.work_dir / "REVIEW_SCOPE_MAP.json"

    def get_allowed_tool_names(self) -> List[str]:
        return list(self.TOOL_NAMES)

    def get_tools(self, work_dir: Path) -> list:
        provider = self

        def load_review_brief() -> str:
            """Load the fixed user question, interpretation, and review scope."""

            raw_query_plan = _read_json(provider.ctx.query_plan_path, {})
            query_plan = (
                raw_query_plan.get("output", raw_query_plan)
                if isinstance(raw_query_plan, dict)
                else {}
            )
            payload = {
                "status": "ok",
                "user_question": provider.ctx.user_question,
                "problem_understanding": provider.ctx.problem_understanding,
                "scope_definition": provider.ctx.scope_definition,
                "query_plan_summary": query_plan,
                "methodology_identity_guidance": {
                    "default": "critical_narrative_review",
                    "allowed": sorted(METHODOLOGY_IDENTITIES),
                    "rule": (
                        "Do not claim a systematic review unless a complete "
                        "search, screening, extraction, and quality protocol exists."
                    ),
                },
                "visual_policy": provider.ctx.visual_policy,
                "topic_identity": provider.ctx.topic_identity,
            }
            return json.dumps(payload, ensure_ascii=True)

        def inspect_review_knowledge_base() -> str:
            """Return corpus-level counts and themes, never citation evidence."""

            return json.dumps(
                {
                    "status": "ok",
                    "overview": _kb_overview(
                        provider.ctx.kb_sqlite,
                        provider.ctx.topic_identity,
                    ),
                },
                ensure_ascii=True,
            )

        def consult_review_mentor(
            categories_json: str,
            planning_question: str,
            max_per_category: int = 3,
        ) -> str:
            """Retrieve transferable writing moves from top-review examples."""

            if (
                provider.ctx.m1_library_path is None
                or not provider.ctx.m1_library_path.exists()
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
                str(category)
                for category in requested
                if str(category) in REVIEW_MENTOR_CATEGORIES
            ]
            if not categories:
                categories = [
                    "problem_reframing",
                    "central_thesis",
                    "taxonomy_design",
                    "section_progression",
                    "synthesis_moves",
                    "figure_argument",
                    "top_journal_publishability",
                ]
            selected = retrieve_mentor_moves(
                provider.ctx.m1_library_path,
                categories=categories,
                planning_question=str(planning_question),
                max_per_category=max_per_category,
            )

            trace = _read_json(provider.mentor_trace_path, {"consultations": []})
            trace.setdefault("consultations", []).append(
                {
                    "planning_question": str(planning_question)[:1000],
                    "categories": categories,
                    "moves_returned": sum(len(v) for v in selected.values()),
                    "usage_rule": (
                        "Writing-architecture instruction only; never scientific "
                        "evidence and never a source of factual claims."
                    ),
                }
            )
            atomic_write_json(provider.mentor_trace_path, trace)
            return json.dumps(
                {
                    "status": "ok",
                    "planning_question": str(planning_question)[:1000],
                    "usage_rule": (
                        "Abstract the organizational move. Do not copy topic facts, "
                        "citations, or conclusions from these examples."
                    ),
                    "mentor_moves": selected,
                },
                ensure_ascii=True,
            )

        def submit_review_blueprint(blueprint_json: str) -> str:
            """Submit the complete intellectual blueprint as one JSON object."""

            try:
                blueprint = json.loads(blueprint_json)
            except Exception as exc:
                return json.dumps(
                    {"status": "error", "error": f"invalid_json: {exc}"},
                    ensure_ascii=True,
                )
            if not isinstance(blueprint, dict):
                return json.dumps(
                    {"status": "error", "error": "blueprint must be an object"},
                    ensure_ascii=True,
                )
            # Accept a small set of semantically unambiguous aliases.  This is
            # schema normalization, not scientific repair: no content,
            # section order, or judgement is invented by the program.
            normalized_sections = []
            for index, raw_section in enumerate(
                blueprint.get("sections", []), start=1
            ):
                if not isinstance(raw_section, dict):
                    normalized_sections.append(raw_section)
                    continue
                section = dict(raw_section)
                section.setdefault(
                    "section_id",
                    section.pop("id", f"S{index:02d}"),
                )
                transitions = section.pop("transitions", {})
                if isinstance(transitions, dict):
                    section.setdefault(
                        "transition_from_previous",
                        transitions.get("from_previous") or "",
                    )
                    section.setdefault(
                        "transition_to_next",
                        transitions.get("to_next") or "",
                    )
                section.setdefault(
                    "visual_argument_slots",
                    section.pop("visual_slots", []),
                )
                required = list(section.get("required_roles", []))
                section.setdefault(
                    "optional_roles",
                    [
                        role
                        for role in COVERAGE_ROLES
                        if role not in required
                    ][:3],
                )
                normalized_sections.append(section)
            blueprint["sections"] = normalized_sections
            rhetorical_contract = blueprint.get("article_rhetorical_contract")
            if not isinstance(rhetorical_contract, dict):
                rhetorical_contract = _legacy_rhetorical_contract(
                    blueprint,
                    provider.ctx,
                )
            else:
                rhetorical_contract = dict(rhetorical_contract)
                rhetorical_contract.setdefault("provenance", "native")
            blueprint["article_rhetorical_contract"] = rhetorical_contract
            blueprint.setdefault(
                "schema_version",
                "research_harness.review_blueprint.v2",
            )
            blueprint["input_context"] = {
                "user_question": provider.ctx.user_question,
                "problem_understanding": provider.ctx.problem_understanding,
                "scope_definition": provider.ctx.scope_definition,
                "query_plan_path": (
                    str(provider.ctx.query_plan_path)
                    if provider.ctx.query_plan_path
                    else ""
                ),
            }
            blueprint["source_review_knowledge_base"] = (
                str(provider.ctx.kb_sqlite) if provider.ctx.kb_sqlite else ""
            )
            # The confirmed Query Planner owns topic identity.  The Review
            # Lead may organize the review but may not replace its subject.
            blueprint["topic_identity"] = dict(provider.ctx.topic_identity)
            # Freeze the review scale at blueprint acceptance.  Coverage and
            # article-level portfolio checks must consume this same contract;
            # they may report a shortfall but may not silently redefine it.
            quality_contract = resolve_review_contract(blueprint)
            blueprint["review_mode"] = quality_contract.mode
            blueprint["reference_target_range"] = list(
                quality_contract.reference_target_range
            )
            blueprint["review_quality_contract"] = quality_contract.to_dict()
            blueprint = attach_review_scope_map(
                blueprint,
                user_question=provider.ctx.user_question,
                query_plan=_read_json(provider.ctx.query_plan_path, {}),
                review_charter=blueprint.get("review_charter"),
                mentor_advice=blueprint.get("review_mentor_advice")
                or blueprint.get("mentor_advice")
                or {},
            )
            blueprint["mentor_usage_rule"] = (
                "M1 writing moves informed architecture only and are not "
                "scientific evidence."
            )
            atomic_write_json(
                provider.scope_map_path,
                blueprint.get("review_scope_map") or {},
            )
            atomic_write_json(provider.blueprint_path, blueprint)
            return json.dumps(
                {
                    "status": "ok",
                    "artifact": provider.blueprint_path.name,
                    "section_count": len(blueprint.get("sections", [])),
                    "rhetorical_contract_provenance": (
                        blueprint.get("article_rhetorical_contract", {}).get(
                            "provenance",
                            "unknown",
                        )
                    ),
                    "scope_map_artifact": provider.scope_map_path.name,
                },
                ensure_ascii=True,
            )

        def validate_review_blueprint_package() -> str:
            """Independently validate architecture, scope, roles, and language."""

            errors: List[str] = []
            warnings: List[str] = []
            blueprint = _read_json(provider.blueprint_path, {})
            if not blueprint:
                return "VALIDATION_FAILED: REVIEW_BLUEPRINT.json is missing."

            methodology = str(
                blueprint.get("methodology_identity") or ""
            ).strip()
            if methodology not in METHODOLOGY_IDENTITIES:
                errors.append("invalid methodology_identity")
            if methodology == "systematic_review" and len(
                str(blueprint.get("methodology_protocol") or "")
            ) < 200:
                errors.append(
                    "systematic_review requires a substantive methodology_protocol"
                )

            rhetorical_contract_raw = blueprint.get(
                "article_rhetorical_contract"
            )
            rhetorical_contract: Optional[ArticleRhetoricalContract] = None
            if not isinstance(rhetorical_contract_raw, dict):
                errors.append("article_rhetorical_contract is missing")
            else:
                try:
                    rhetorical_contract = (
                        ArticleRhetoricalContract.model_validate(
                            rhetorical_contract_raw
                        )
                    )
                except ValidationError as exc:
                    compact = "; ".join(
                        ".".join(str(part) for part in item["loc"])
                        + ": "
                        + item["msg"]
                        for item in exc.errors()[:8]
                    )
                    errors.append(
                        "article_rhetorical_contract is invalid: " + compact
                    )
                else:
                    if (
                        rhetorical_contract.methodology_identity
                        != methodology
                    ):
                        errors.append(
                            "article_rhetorical_contract methodology disagrees "
                            "with blueprint methodology_identity"
                        )
                    if (
                        rhetorical_contract.provenance
                        == "legacy_compatibility"
                    ):
                        warnings.append(
                            "article_rhetorical_contract was derived for legacy "
                            "compatibility; a new real run should submit it natively"
                        )
                    contract_text = json.dumps(
                        rhetorical_contract.model_dump(mode="json"),
                        ensure_ascii=False,
                    )
                    if _CJK.search(contract_text):
                        errors.append(
                            "article_rhetorical_contract contains CJK text"
                        )
                    if (
                        len(
                            rhetorical_contract.body_contract.primary_taxonomy
                        )
                        < 30
                    ):
                        errors.append(
                            "article_rhetorical_contract primary_taxonomy is too short"
                        )
                    word_range = (
                        rhetorical_contract.abstract_contract.target_word_range
                    )
                    if (
                        word_range.min < 120
                        or word_range.max < word_range.min
                        or word_range.max > 500
                    ):
                        errors.append(
                            "article_rhetorical_contract abstract word range "
                            "is unrealistic"
                        )
                    if (
                        not rhetorical_contract.scope_inclusions
                        or not rhetorical_contract.scope_exclusions
                    ):
                        errors.append(
                            "article_rhetorical_contract must state scope "
                            "inclusions and exclusions"
                        )

            for field_name in (
                "review_thesis",
                "full_review_argument",
                "taxonomy_principle",
                "narrative_strategy",
            ):
                value = str(blueprint.get(field_name) or "").strip()
                if len(value) < 40:
                    errors.append(f"{field_name} is missing or too short")
                if _CJK.search(value):
                    errors.append(f"{field_name} contains CJK text")

            serialized = json.dumps(blueprint, ensure_ascii=False)
            if _CJK.search(serialized):
                errors.append("blueprint contains CJK text")
            if "source_paper_id" in serialized or "evidence_locator" in serialized:
                errors.append(
                    "M1 source records leaked into blueprint as if they were evidence"
                )
            topic_identity = provider.ctx.topic_identity
            topic_alignment: Dict[str, Any]
            if not topic_identity.get("valid"):
                topic_alignment = {
                    "status": "failed",
                    "reason": "topic_identity_unavailable",
                }
                errors.append("topic_identity contract is missing or invalid")
            else:
                topic_alignment = assess_topic_alignment(
                    [
                        blueprint.get("review_thesis", ""),
                        blueprint.get("full_review_argument", ""),
                        blueprint.get("taxonomy_principle", ""),
                        blueprint.get("sections", []),
                    ],
                    topic_identity,
                    strict=True,
                )
                if topic_alignment["status"] != "passed":
                    errors.append(
                        "blueprint does not preserve the confirmed scientific object"
                    )

            sections = blueprint.get("sections")
            if not isinstance(sections, list) or not 5 <= len(sections) <= 12:
                errors.append("sections must contain 5 to 12 entries")
                sections = sections if isinstance(sections, list) else []

            seen_ids = set()
            seen_titles = set()
            all_required_roles = set()
            visual_slot_count = 0
            section_topic_alignments: Dict[str, Any] = {}
            for index, section in enumerate(sections, start=1):
                if not isinstance(section, dict):
                    errors.append(f"section[{index}] is not an object")
                    continue
                expected_id = f"S{index:02d}"
                section_id = str(section.get("section_id") or "")
                if section_id != expected_id or section_id in seen_ids:
                    errors.append(
                        f"section[{index}] must use unique id {expected_id}"
                    )
                seen_ids.add(section_id)
                title = str(section.get("title") or "").strip()
                if len(title) < 8 or title.lower() in seen_titles:
                    errors.append(f"{section_id} has missing/duplicate title")
                seen_titles.add(title.lower())
                # ``argument_role`` is a compact functional contract, whereas
                # ``chapter_argument`` carries the substantive proposition.
                # Requiring both to exceed the same arbitrary length made a
                # concise but valid role trigger another expensive A-model
                # rewrite.  Keep the substantive field strict and accept a
                # semantically useful concise role.
                if len(str(section.get("argument_role") or "").strip()) < 20:
                    errors.append(f"{section_id}.argument_role is too short")
                if len(str(section.get("chapter_argument") or "").strip()) < 40:
                    errors.append(f"{section_id}.chapter_argument is too short")
                questions = section.get("key_questions")
                if not isinstance(questions, list) or not 1 <= len(questions) <= 5:
                    errors.append(f"{section_id}.key_questions must have 1-5 items")
                required = section.get("required_roles")
                optional = section.get("optional_roles", [])
                if (
                    not isinstance(required, list)
                    or not 1 <= len(required) <= 5
                    or any(role not in COVERAGE_ROLES for role in required)
                ):
                    errors.append(f"{section_id}.required_roles is invalid")
                    required = []
                if (
                    not isinstance(optional, list)
                    or any(role not in COVERAGE_ROLES for role in optional)
                ):
                    errors.append(f"{section_id}.optional_roles is invalid")
                all_required_roles.update(required)
                if len(str(section.get("synthesis_task") or "")) < 30:
                    errors.append(f"{section_id}.synthesis_task is too short")
                if len(str(section.get("mentor_guidance") or "")) < 30:
                    errors.append(f"{section_id}.mentor_guidance is too short")
                if topic_identity.get("valid"):
                    section_topic_alignment = assess_topic_alignment(
                        [
                            title,
                            section.get("chapter_argument", ""),
                            section.get("key_questions", []),
                            section.get("synthesis_task", ""),
                        ],
                        topic_identity,
                        strict=False,
                    )
                    section_topic_alignments[section_id] = (
                        section_topic_alignment
                    )
                    if section_topic_alignment["status"] != "passed":
                        errors.append(
                            f"{section_id} does not preserve the scientific "
                            "object in its title, argument, questions, and "
                            "synthesis task"
                        )
                if not isinstance(section.get("scope_guardrails"), list):
                    errors.append(f"{section_id}.scope_guardrails must be a list")
                slots = section.get("visual_argument_slots", [])
                if not isinstance(slots, list):
                    errors.append(
                        f"{section_id}.visual_argument_slots must be a list"
                    )
                else:
                    visual_slot_count += len(slots)
                target = section.get("target_word_range", {})
                if not isinstance(target, dict):
                    errors.append(f"{section_id}.target_word_range is invalid")
                else:
                    lower = int(target.get("min", 0) or 0)
                    upper = int(target.get("max", 0) or 0)
                    if lower < 700 or upper < lower or upper > 5000:
                        errors.append(
                            f"{section_id}.target_word_range is unrealistic"
                        )

            if not {"foundation", "mechanism", "method", "frontier"}.issubset(
                all_required_roles
            ):
                errors.append(
                    "review-wide required roles must cover foundation, mechanism, "
                    "method, and frontier"
                )
            if not ({"controversy", "application"} & all_required_roles):
                warnings.append(
                    "neither controversy nor application is required anywhere"
                )
            if visual_slot_count < 3:
                errors.append(
                    "review blueprint needs at least three argumentative visual slots"
                )

            report = {
                "schema_version": "research_harness.blueprint_validation.v1",
                "status": "passed" if not errors else "failed",
                "errors": errors,
                "warnings": warnings,
                "section_count": len(sections),
                "required_roles_covered": sorted(all_required_roles),
                "visual_slot_count": visual_slot_count,
                "article_rhetorical_contract_status": (
                    "valid" if rhetorical_contract is not None else "invalid"
                ),
                "article_rhetorical_contract_provenance": (
                    rhetorical_contract.provenance
                    if rhetorical_contract is not None
                    else "unknown"
                ),
                "topic_alignment": topic_alignment,
                "section_topic_alignments": section_topic_alignments,
            }
            atomic_write_json(provider.validation_path, report)
            if errors:
                return "VALIDATION_FAILED: " + "; ".join(errors[:12])
            return (
                "VALIDATION_PASSED: review blueprint has a coherent architecture, "
                f"{len(sections)} sections, and {visual_slot_count} visual slots."
            )

        return [
            FunctionTool(load_review_brief),
            FunctionTool(inspect_review_knowledge_base),
            FunctionTool(consult_review_mentor),
            FunctionTool(submit_review_blueprint),
            FunctionTool(validate_review_blueprint_package),
        ]
