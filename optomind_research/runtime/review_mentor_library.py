"""Retrieval helpers for the M1 top-review writing mentor library.

The M1 corpus teaches transferable intellectual and editorial moves.  It is
never a source of scientific facts for the review topic.  This module keeps
that boundary explicit while allowing both the Review Lead and Managing
Editor to retrieve moves relevant to their *writing problem* instead of always
receiving the same globally high-ranked records.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

REVIEW_MENTOR_CATEGORIES = (
    "problem_reframing",
    "central_thesis",
    "taxonomy_design",
    "synthesis_moves",
    "section_progression",
    "paragraph_moves",
    "evidence_critique",
    "disagreement_handling",
    "gap_characterization",
    "figure_argument",
    "top_journal_publishability",
)

_STOPWORDS = {
    "about", "after", "also", "article", "before", "between", "could",
    "current", "from", "have", "into", "more", "paper", "review", "section",
    "should", "that", "their", "these", "this", "those", "through", "using",
    "what", "when", "where", "which", "with", "would", "write", "writing",
}
_WORD = re.compile(r"[a-z][a-z0-9_-]{2,}")

_CATEGORY_INTENT = {
    "problem_reframing": {
        "reframe", "question", "problem", "challenge", "premise", "scope",
    },
    "central_thesis": {
        "thesis", "argument", "claim", "position", "conclusion", "verdict",
    },
    "taxonomy_design": {
        "taxonomy", "classification", "organize", "axis", "mechanism",
        "category", "route",
    },
    "synthesis_moves": {
        "synthesis", "integrate", "compare", "pattern", "judgment", "combine",
    },
    "section_progression": {
        "progression", "transition", "sequence", "chapter", "section",
        "repeat", "flow",
    },
    "paragraph_moves": {
        "paragraph", "topic", "sentence", "local", "explain", "develop",
    },
    "evidence_critique": {
        "evidence", "quality", "bias", "limit", "methodology", "reliability",
    },
    "disagreement_handling": {
        "disagreement", "conflict", "controversy", "contradiction",
        "reconcile", "boundary",
    },
    "gap_characterization": {
        "gap", "unknown", "missing", "future", "open", "uncertainty",
    },
    "figure_argument": {
        "figure", "visual", "diagram", "table", "image", "roadmap",
    },
    "top_journal_publishability": {
        "publishability", "novelty", "editor", "reviewer", "contribution",
        "impact", "journal",
    },
}


def _tokens(value: Any) -> set[str]:
    return {
        token
        for token in _WORD.findall(str(value or "").lower())
        if token not in _STOPWORDS
    }


def _load(path: Optional[Path]) -> Dict[str, Any]:
    if path is None or not Path(path).exists():
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _quality_score(move: Dict[str, Any]) -> float:
    confidence = {"high": 3.0, "medium": 1.8, "low": 0.5}.get(
        str(move.get("confidence", "")).lower(),
        0.0,
    )
    genre = {
        "review": 3.0,
        "perspective": 2.6,
        "roadmap": 2.6,
        "primary_article": 0.5,
    }.get(str(move.get("source_genre", "")).lower(), 0.0)
    adequacy = {"pass": 2.5, "borderline": 0.8}.get(
        str(move.get("adequacy_for_batch_library", "")).lower(),
        0.0,
    )
    explanatory = min(
        len(str(move.get("why_it_matters", "")))
        + len(str(move.get("reuse_for_our_review_system", ""))),
        1600,
    ) / 800.0
    return confidence + genre + adequacy + explanatory


def _move_tokens(move: Dict[str, Any]) -> set[str]:
    # Source titles and evidence locators are deliberately excluded.  They
    # carry topic facts and can overpower the transferable writing move.
    return _tokens(
        " ".join(
            str(move.get(field) or "")
            for field in (
                "move",
                "why_it_matters",
                "possible_overreach",
                "reuse_for_our_review_system",
            )
        )
    )


def retrieve_mentor_moves(
    library_path: Optional[Path],
    *,
    categories: Iterable[str],
    planning_question: str,
    max_per_category: int = 3,
) -> Dict[str, List[Dict[str, Any]]]:
    """Return writing-problem-aware, source-diverse mentor moves.

    The returned records intentionally omit source IDs, evidence locators, and
    source titles so downstream agents cannot accidentally cite M1 as topical
    evidence.
    """

    library = _load(library_path)
    if not library:
        return {}
    requested = [
        str(category)
        for category in categories
        if str(category) in REVIEW_MENTOR_CATEGORIES
    ]
    if not requested:
        requested = list(REVIEW_MENTOR_CATEGORIES)
    per_category = min(max(1, int(max_per_category)), 4)
    question_tokens = _tokens(planning_question)
    selected: Dict[str, List[Dict[str, Any]]] = {}
    used_sources: set[str] = set()

    for category in requested:
        intent_tokens = set(_CATEGORY_INTENT.get(category, set()))
        query_tokens = question_tokens | intent_tokens
        ranked: List[tuple[float, Dict[str, Any]]] = []
        for move in library.get(category, []):
            if not isinstance(move, dict):
                continue
            overlap = len(query_tokens & _move_tokens(move))
            # Relevance influences order but cannot bury a high-quality,
            # transferable move merely because it uses different vocabulary.
            score = _quality_score(move) + min(overlap, 8) * 1.4
            ranked.append((score, move))
        ranked.sort(key=lambda item: item[0], reverse=True)

        chosen: List[Dict[str, Any]] = []
        deferred_duplicates: List[tuple[float, Dict[str, Any]]] = []
        for score, move in ranked:
            source_id = str(move.get("source_paper_id") or "")
            if source_id and source_id in used_sources:
                deferred_duplicates.append((score, move))
                continue
            chosen.append(_public_move(move, score))
            if source_id:
                used_sources.add(source_id)
            if len(chosen) >= per_category:
                break
        # Do not leave a category empty solely because all its best moves came
        # from sources already used for another category.
        for score, move in deferred_duplicates:
            if len(chosen) >= per_category:
                break
            chosen.append(_public_move(move, score))
        selected[category] = chosen
    return selected


def _public_move(move: Dict[str, Any], score: float) -> Dict[str, Any]:
    return {
        "move": move.get("move", ""),
        "why_it_matters": move.get("why_it_matters", ""),
        "possible_overreach": move.get("possible_overreach", ""),
        "reuse_guidance": move.get("reuse_for_our_review_system", ""),
        "source_genre": move.get("source_genre", ""),
        "retrieval_score": round(float(score), 3),
    }
