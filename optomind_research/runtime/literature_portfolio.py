"""Deterministic article-level literature portfolio coordination.

The section researcher decides whether an individual source is useful.  This
module performs the different, deterministic job of checking whether the
article as a whole has enough independent sources and whether a small number
of papers dominate too many section roles.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

from .artifact_store import atomic_write_json
from .review_quality_contract import resolve_review_contract
from .coverage_atlas import build_coverage_atlas


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _planned_word_count(blueprint: Dict[str, Any]) -> int:
    total = 0
    for section in blueprint.get("sections", []):
        if not isinstance(section, dict):
            continue
        word_range = section.get("target_word_range") or {}
        try:
            total += int(word_range.get("min") or 0)
        except (TypeError, ValueError):
            pass
    return total


def _section_paths(
    coverage_root: Path,
    blueprint: Dict[str, Any],
) -> Iterable[tuple[str, Path]]:
    for section in blueprint.get("sections", []):
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("section_id") or "")
        if section_id:
            yield section_id, coverage_root / "sections" / section_id


def build_literature_portfolio_report(
    *,
    blueprint: Dict[str, Any],
    coverage_root: Path,
    output_path: Path | None = None,
) -> Dict[str, Any]:
    """Build an auditable article/section source-diversity report."""

    article_papers: set[str] = set()
    article_direct_papers: set[str] = set()
    paper_sections: dict[str, set[str]] = defaultdict(set)
    paper_role_uses: Counter[str] = Counter()
    section_reports = []
    expansion_sections = []

    blueprint_sections = [
        item for item in blueprint.get("sections", [])
        if isinstance(item, dict)
    ]
    section_count = len(blueprint_sections)
    review_contract = resolve_review_contract(blueprint)
    section_by_id = {
        str(item.get("section_id") or ""): item for item in blueprint_sections
    }

    for section_id, section_dir in _section_paths(coverage_root, blueprint):
        package = _read_json(section_dir / "SECTION_MATERIAL_PACKAGE.json")
        ledger = _read_json(section_dir / "SECTION_SOURCE_LEDGER.json")
        sources = [
            source
            for source in ledger.get("sources", [])
            if isinstance(source, dict)
            and source.get("paper_id")
            and source.get("canonical_chunk_ids")
            and source.get("scope_fit") in {"direct", "adjacent"}
        ]
        unique = {str(source["paper_id"]) for source in sources}
        direct = {
            str(source["paper_id"])
            for source in sources
            if source.get("scope_fit") == "direct"
        }
        role_uses = Counter(str(source.get("paper_id")) for source in sources)
        largest_role_share = (
            max(role_uses.values()) / max(1, sum(role_uses.values()))
            if role_uses
            else 0.0
        )
        contract_targets = review_contract.section_targets(
            section=section_by_id.get(section_id, {}),
            section_count=section_count,
        )
        minimum_unique = int(
            package.get("minimum_unique_sources")
            or contract_targets["minimum_unique_sources"]
            or 0
        )
        minimum_direct = int(
            package.get("minimum_direct_sources")
            or contract_targets["minimum_direct_sources"]
            or 0
        )
        breadth_met = (
            len(unique) >= minimum_unique
            and len(direct) >= minimum_direct
        )
        concentration_risk = (
            len(unique) >= 3 and largest_role_share > 0.40
        )
        missing_unique = max(0, minimum_unique - len(unique))
        missing_direct = max(0, minimum_direct - len(direct))
        needs_expansion = (
            not breadth_met
            or concentration_risk
            or bool(package.get("blocking_gaps_remain"))
        )
        reasons = []
        if missing_unique:
            reasons.append(f"needs_{missing_unique}_additional_unique_sources")
        if missing_direct:
            reasons.append(f"needs_{missing_direct}_additional_direct_sources")
        if concentration_risk:
            reasons.append("single_source_role_concentration")
        if package.get("blocking_gaps_remain"):
            reasons.append("blocking_coverage_gap")

        row = {
            "section_id": section_id,
            "unique_sources": len(unique),
            "direct_sources": len(direct),
            "minimum_unique_sources": minimum_unique,
            "minimum_direct_sources": minimum_direct,
            "target_source": (
                "section_material_package"
                if package.get("minimum_unique_sources")
                else "review_mode_contract"
            ),
            "breadth_target_met": breadth_met,
            "largest_source_role_share": round(largest_role_share, 4),
            "concentration_risk": concentration_risk,
            "needs_expansion": needs_expansion,
            "expansion_reasons": reasons,
            "top_role_sources": [
                {"paper_id": paper_id, "role_count": count}
                for paper_id, count in role_uses.most_common(5)
            ],
        }
        section_reports.append(row)
        if needs_expansion:
            expansion_sections.append(section_id)

        article_papers.update(unique)
        article_direct_papers.update(direct)
        for source in sources:
            paper_id = str(source["paper_id"])
            paper_sections[paper_id].add(section_id)
            paper_role_uses[paper_id] += 1

    planned_words = _planned_word_count(blueprint)
    # The article target comes from the same contract as section targets.  It
    # is a target, not an artificial hard quota: if OA scarcity prevents it,
    # the report records the shortfall and the reason instead of pretending the
    # article is broad enough.
    recommended_unique = review_contract.minimum_references
    recommended_direct = max(
        1,
        math.ceil(recommended_unique * 0.60),
    )
    largest_article_share = (
        max(paper_role_uses.values()) / max(1, sum(paper_role_uses.values()))
        if paper_role_uses
        else 0.0
    )
    article_target_met = (
        len(article_papers) >= recommended_unique
        and len(article_direct_papers) >= recommended_direct
    )

    report = {
        "schema_version": "research_harness.literature_portfolio.v1",
        "created_at": _now(),
        "planned_word_count": planned_words,
        "section_count": section_count,
        "review_mode": review_contract.mode,
        "review_mode_contract": review_contract.to_dict(),
        "article_unique_sources": len(article_papers),
        "article_direct_sources": len(article_direct_papers),
        "recommended_minimum_unique_sources": recommended_unique,
        "recommended_minimum_direct_sources": recommended_direct,
        "article_breadth_target_met": article_target_met,
        "article_reference_shortfall": {
            "unique_sources": max(0, recommended_unique - len(article_papers)),
            "direct_sources": max(0, recommended_direct - len(article_direct_papers)),
            "is_reported_not_hidden": True,
        },
        "largest_article_source_role_share": round(largest_article_share, 4),
        "sections_needing_expansion": expansion_sections,
        "section_reports": section_reports,
        "coverage_atlas": build_coverage_atlas(
            blueprint=blueprint,
            coverage_root=coverage_root,
        ),
        "top_reused_sources": [
            {
                "paper_id": paper_id,
                "section_count": len(paper_sections[paper_id]),
                "role_count": count,
            }
            for paper_id, count in paper_role_uses.most_common(12)
        ],
        "stopping_policy": (
            "A bounded retry is justified for sections below explicit breadth "
            "targets or with severe role concentration. Failure to find a new "
            "usable source after strategically distinct searches is recorded, "
            "not hidden and not retried indefinitely."
        ),
    }
    if output_path is not None:
        atomic_write_json(output_path, report)
    return report


def build_portfolio_feedback(
    report: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """Translate deterministic portfolio findings into researcher feedback."""

    feedback: Dict[str, Dict[str, Any]] = {}
    section_rows = [
        row
        for row in report.get("section_reports", [])
        if isinstance(row, dict) and row.get("section_id")
    ]
    selected_ids = {
        str(row.get("section_id"))
        for row in section_rows
        if row.get("needs_expansion")
    }
    # Section-level minima can all pass while the complete review still cites
    # only a small, heavily reused corpus.  Select a bounded set of the thinnest
    # chapters for independent-source expansion instead of merely reporting the
    # article-level failure.
    if not report.get("article_breadth_target_met", False):
        unique_shortfall = max(
            0,
            int(report.get("recommended_minimum_unique_sources") or 0)
            - int(report.get("article_unique_sources") or 0),
        )
        direct_shortfall = max(
            0,
            int(report.get("recommended_minimum_direct_sources") or 0)
            - int(report.get("article_direct_sources") or 0),
        )
        requested_count = min(
            len(section_rows),
            4,
            max(2, math.ceil(max(unique_shortfall, direct_shortfall) / 3)),
        )
        ranked = sorted(
            section_rows,
            key=lambda row: (
                int(row.get("direct_sources") or 0),
                int(row.get("unique_sources") or 0),
                -float(row.get("largest_source_role_share") or 0.0),
                str(row.get("section_id") or ""),
            ),
        )
        selected_ids.update(
            str(row["section_id"]) for row in ranked[:requested_count]
        )

    for row in section_rows:
        section_id = str(row.get("section_id") or "")
        if section_id not in selected_ids:
            continue
        if not isinstance(row, dict) or not row.get("needs_expansion"):
            reasons = ["article_level_source_breadth_shortfall"]
        else:
            reasons = list(row.get("expansion_reasons") or [])
        if not report.get("article_breadth_target_met", False):
            reasons.append("article_level_source_breadth_shortfall")
        reasons = list(dict.fromkeys(reasons))
        feedback[section_id] = {
            "schema_version": "research_harness.coverage_feedback.v1",
            "feedback_origin": "pre_authoring_literature_portfolio",
            "feedback_items": [
                {
                    "gap_type": "source_breadth",
                    "role": "",
                    "description": (
                        "Add independent, directly relevant papers that perform "
                        "the section's existing literature roles. Prefer a new "
                        "paper over another chunk from an already adopted paper."
                    ),
                    "required_outcome": {
                        "minimum_unique_sources": row.get(
                            "minimum_unique_sources", 0
                        ),
                        "minimum_direct_sources": row.get(
                            "minimum_direct_sources", 0
                        ),
                        "maximum_preferred_single_source_role_share": 0.40,
                        "article_recommended_unique_sources": report.get(
                            "recommended_minimum_unique_sources", 0
                        ),
                        "article_current_unique_sources": report.get(
                            "article_unique_sources", 0
                        ),
                    },
                    "reasons": reasons,
                }
            ],
        }
    return feedback
