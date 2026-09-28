"""The canonical review problem-space contract.

``ReviewScopeMap`` is intentionally deterministic.  It does not replace the
Query Planner, Review Charter, or M1 mentor.  It reconciles their outputs into
one object that downstream discovery and section coverage can consume.  M1 is
recorded as an architectural advisor only; it is never treated as scientific
evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


LITERATURE_ROLES = (
    "foundation",
    "definition",
    "mechanism",
    "method",
    "validation",
    "comparison",
    "review",
    "controversy",
    "boundary",
    "frontier",
    "application",
)

RELATION_TASKS = (
    "progression",
    "complementarity",
    "controversy",
    "tradeoff",
    "boundary",
)


def _text(value: Any, limit: int = 1200) -> str:
    return str(value or "").strip()[:limit]


def _unique(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(_text(value, 500) for value in values if _text(value, 500)))


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _query_output(query_plan: dict[str, Any]) -> dict[str, Any]:
    output = query_plan.get("output")
    return output if isinstance(output, dict) else query_plan


def _scope_items(scope: Any) -> tuple[str, list[str], list[str]]:
    if isinstance(scope, str):
        return _text(scope), [], []
    if not isinstance(scope, dict):
        return "", [], []
    main = _text(
        scope.get("main_scope")
        or scope.get("scope_statement")
        or scope.get("description")
    )
    included = _unique(
        scope.get("scope_items")
        or scope.get("inclusions")
        or scope.get("included_topics")
        or []
    )
    excluded = _unique(
        scope.get("exclusions")
        or scope.get("excluded_topics")
        or scope.get("scope_exclusions")
        or []
    )
    return main, included, excluded


def _dimensions_from_blueprint(blueprint: dict[str, Any]) -> list[dict[str, Any]]:
    dimensions: list[dict[str, Any]] = []
    for section in blueprint.get("sections") or []:
        if not isinstance(section, dict):
            continue
        section_id = _text(section.get("section_id"), 80)
        if not section_id:
            continue
        roles = _unique(
            list(section.get("required_roles") or [])
            + list(section.get("optional_roles") or [])
        )
        relation_tasks = [
            item
            for item in _unique(section.get("relationship_tasks") or [])
            if item in RELATION_TASKS
        ]
        dimensions.append(
            {
                "dimension_id": section_id,
                "title": _text(section.get("title") or section.get("section_title"), 300),
                "argument_task": _text(
                    section.get("chapter_argument")
                    or section.get("argument_role")
                    or section.get("synthesis_task")
                ),
                "inclusion_boundary": _text(
                    section.get("scope_definition")
                    or section.get("scope_description")
                    or section.get("scope_guardrails")
                ),
                "literature_roles": roles,
                "relation_tasks": relation_tasks,
                "source": "review_blueprint_section",
            }
        )
    return dimensions


@dataclass(slots=True)
class ReviewScopeMap:
    schema_version: str = "review_scope_map.v1"
    article_identity: str = ""
    review_mode: str = "critical_narrative_review"
    user_question: str = ""
    problem_understanding: str = ""
    core_question: str = ""
    central_judgment: str = ""
    research_dimensions: list[dict[str, Any]] = field(default_factory=list)
    inclusion_boundaries: list[str] = field(default_factory=list)
    exclusion_boundaries: list[str] = field(default_factory=list)
    # These are the roles/tasks actually requested by the current review
    # scope.  They are intentionally not the complete global vocabulary:
    # discovery must not manufacture obligations for unrelated dimensions.
    literature_roles: list[str] = field(default_factory=list)
    required_literature_roles: list[str] = field(default_factory=list)
    relation_tasks: list[str] = field(default_factory=list)
    search_anchors: list[str] = field(default_factory=list)
    m1_architecture_guidance: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    unresolved_items: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_review_scope_map(
    *,
    user_question: str = "",
    query_plan: dict[str, Any] | None = None,
    review_charter: dict[str, Any] | None = None,
    mentor_advice: dict[str, Any] | None = None,
    blueprint: dict[str, Any] | None = None,
) -> ReviewScopeMap:
    """Reconcile existing upstream artifacts into one auditable scope map."""

    query_plan = _as_dict(query_plan)
    query_output = _query_output(query_plan)
    charter = _as_dict(review_charter)
    blueprint = _as_dict(blueprint)
    mentor = _as_dict(mentor_advice)
    input_context = _as_dict(blueprint.get("input_context"))

    question = _text(
        user_question
        or query_output.get("user_question")
        or input_context.get("user_question")
        or blueprint.get("central_question")
    )
    problem = _text(
        query_output.get("problem_understanding")
        or input_context.get("problem_understanding")
        or blueprint.get("problem_understanding")
        or question
    )
    scope_main, scope_included, scope_excluded = _scope_items(
        query_output.get("scope_definition")
        or query_output.get("scope_definition_en")
        or blueprint.get("scope_definition")
    )
    charter_scope, charter_included, charter_excluded = _scope_items(
        charter.get("scope_statement")
        or charter.get("scope_inclusions")
        or blueprint.get("scope_statement")
    )
    inclusions = _unique(scope_included + charter_included)
    exclusions = _unique(scope_excluded + charter_excluded)
    if scope_main:
        inclusions.insert(0, scope_main)
    if charter_scope and charter_scope not in inclusions:
        inclusions.insert(0, charter_scope)

    keyword_block = _as_dict(query_output.get("keyword_decomposition"))
    anchors = _unique(
        list(keyword_block.get("keywords") or [])
        + list(keyword_block.get("positive_keywords") or [])
        + list(keyword_block.get("retrieval_terms") or [])
    )
    dimensions = _dimensions_from_blueprint(blueprint)
    if not dimensions:
        raw_dimensions = blueprint.get("research_dimensions") or charter.get(
            "research_dimensions"
        )
        for index, item in enumerate(raw_dimensions or [], start=1):
            if isinstance(item, str):
                dimensions.append(
                    {
                        "dimension_id": f"D{index:02d}",
                        "title": item,
                        "argument_task": item,
                        "literature_roles": [],
                        "relation_tasks": [],
                        "source": "upstream_dimension",
                    }
                )
            elif isinstance(item, dict):
                row = dict(item)
                row.setdefault("dimension_id", f"D{index:02d}")
                row.setdefault("source", "upstream_dimension")
                dimensions.append(row)

    review_mode = _text(
        blueprint.get("review_mode")
        or charter.get("review_mode")
        or charter.get("methodology_identity")
        or "critical_narrative_review",
        80,
    )
    identity = _text(
        charter.get("target_article_type")
        or blueprint.get("target_article_type")
        or "scientific literature review",
        240,
    )
    judgment = _text(
        blueprint.get("review_thesis")
        or blueprint.get("central_judgment")
        or blueprint.get("full_review_argument")
        or charter.get("review_thesis")
        or "",
    )

    m1_guidance: list[str] = []
    moves = mentor.get("usable_intellectual_moves") or mentor.get("mentor_moves") or []
    if isinstance(moves, dict):
        moves = [move for values in moves.values() if isinstance(values, list) for move in values]
    for move in moves:
        if isinstance(move, dict):
            text = _text(
                move.get("move")
                or move.get("action")
                or move.get("reuse_for_our_review_system")
                or move.get("description")
            )
        else:
            text = _text(move)
        if text:
            m1_guidance.append(text)
    if not m1_guidance:
        mentor_block = blueprint.get("review_mentor_advice")
        fallback_guidance = blueprint.get("m1_patterns_applied") or []
        if isinstance(mentor_block, dict):
            fallback_guidance = fallback_guidance or mentor_block.get(
                "planning_principles", []
            )
        m1_guidance = _unique(fallback_guidance)

    active_roles = _unique(
        role
        for dimension in dimensions
        for role in (dimension.get("literature_roles") or [])
        if str(role) in LITERATURE_ROLES
    )
    if not active_roles:
        active_roles = _unique(
            role
            for role in (
                blueprint.get("required_literature_roles") or []
            )
            if str(role) in LITERATURE_ROLES
        )
    active_relations = _unique(
        relation
        for dimension in dimensions
        for relation in (dimension.get("relation_tasks") or [])
        if str(relation) in RELATION_TASKS
    )
    if not active_relations:
        active_relations = _unique(
            relation
            for relation in (blueprint.get("relation_tasks") or [])
            if str(relation) in RELATION_TASKS
        )

    unresolved: list[str] = []
    # The scope map is the first durable contract consumed by discovery.  A
    # truthy ``query_plan`` alone is not enough: older runs sometimes contain
    # a wrapper or a partial plan.  Record each missing planning ingredient so
    # the acceptance report and downstream controller can fail conservatively.
    if not query_plan:
        unresolved.append("query_planner_artifact_missing")
    if not _text(query_output.get("problem_understanding")):
        unresolved.append("query_planner_problem_understanding_missing")
    if not scope_main and not inclusions:
        unresolved.append("query_planner_scope_definition_missing")
    if not anchors:
        unresolved.append("query_planner_search_anchors_missing")
    if not exclusions:
        unresolved.append("scope_exclusion_boundary_not_declared")
    if not question:
        unresolved.append("missing_user_question")
    if not dimensions:
        unresolved.append("missing_research_dimensions_until_blueprint_stage")
    if not judgment:
        unresolved.append("central_judgment_not_yet_decided")

    return ReviewScopeMap(
        article_identity=identity,
        review_mode=review_mode,
        user_question=question,
        problem_understanding=problem,
        core_question=question or problem,
        central_judgment=judgment,
        research_dimensions=dimensions,
        inclusion_boundaries=inclusions,
        exclusion_boundaries=exclusions,
        literature_roles=active_roles,
        required_literature_roles=active_roles,
        relation_tasks=active_relations,
        search_anchors=anchors,
        m1_architecture_guidance=_unique(m1_guidance),
        provenance={
            "query_planner": bool(query_plan),
            "review_charter": bool(charter),
            "m1_mentor": bool(mentor),
            "review_blueprint": bool(blueprint),
            "m1_is_architecture_only": True,
        },
        unresolved_items=unresolved,
    )


def attach_review_scope_map(
    blueprint: dict[str, Any],
    *,
    user_question: str = "",
    query_plan: dict[str, Any] | None = None,
    review_charter: dict[str, Any] | None = None,
    mentor_advice: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a copy of the blueprint with the canonical map attached."""

    result = dict(blueprint or {})
    scope_map = build_review_scope_map(
        user_question=user_question,
        query_plan=query_plan,
        review_charter=review_charter,
        mentor_advice=mentor_advice,
        blueprint=result,
    )
    result["review_scope_map"] = scope_map.to_dict()
    for section in result.get("sections") or []:
        if isinstance(section, dict):
            section.setdefault("review_scope_map", scope_map.to_dict())
    return result
