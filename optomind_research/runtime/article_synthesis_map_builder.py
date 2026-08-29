"""Build and validate the bounded input for article-wide synthesis.

The module is intentionally split into deterministic collection and model
judgement.  It never treats handoff cards as evidence and never trusts IDs
returned by a language model without checking them against section artifacts.
"""

from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set, Tuple

from .article_completion_schemas import (
    ArticleSynthesisMap,
    SectionHandoffCard,
)
from .artifact_store import atomic_write_json

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _bounded_draft_memory(text: str, limit: int = 2600) -> str:
    """Retain the opening and closing argument without shipping full prose."""

    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    half = max(200, (limit - 40) // 2)
    return cleaned[:half].rstrip() + " ... [middle omitted] ... " + cleaned[-half:].lstrip()


def _verified_ids(section_dir: Path) -> Tuple[List[str], List[str], List[str]]:
    citation_map = _read_json(section_dir / "SECTION_CITATION_MAP.json", {})
    citations = (
        citation_map.get("citations", [])
        if isinstance(citation_map, dict)
        else []
    )
    paper_ids = sorted(
        {
            str(paper_id)
            for citation in citations
            if isinstance(citation, dict)
            for paper_id in citation.get("paper_ids", [])
            if str(paper_id).strip()
        }
    )
    chunk_ids = sorted(
        {
            str(chunk_id)
            for citation in citations
            if isinstance(citation, dict)
            for chunk_id in citation.get("chunk_ids", [])
            if str(chunk_id).strip()
        }
    )
    placement = _read_json(
        section_dir / "SECTION_VISUAL_PLACEMENT.json",
        {},
    )
    visual_ids = sorted(
        {
            str(item.get("visual_chunk_id"))
            for item in (
                placement.get("placements", [])
                if isinstance(placement, dict)
                else []
            )
            if isinstance(item, dict)
            and str(item.get("visual_chunk_id") or "").strip()
            and str(item.get("asset_status") or "") == "verified_local"
        }
    )
    return paper_ids, chunk_ids, visual_ids


def _load_handoff(
    section: Dict[str, Any],
    section_dir: Path,
) -> Tuple[Dict[str, Any], str]:
    raw = _read_json(section_dir / "SECTION_HANDOFF_CARD.json", {})
    paper_ids, chunk_ids, visual_ids = _verified_ids(section_dir)
    if isinstance(raw, dict) and raw:
        raw = dict(raw)
        raw["section_id"] = str(section.get("section_id") or "")
        raw["section_title"] = str(section.get("title") or "")
        raw["used_paper_ids"] = paper_ids
        raw["used_chunk_ids"] = chunk_ids
        raw["visual_takeaways"] = [
            item
            for item in raw.get("visual_takeaways", [])
            if isinstance(item, dict)
            and str(item.get("visual_chunk_id") or "") in set(visual_ids)
        ]
        try:
            return (
                SectionHandoffCard.model_validate(raw).model_dump(mode="json"),
                "native",
            )
        except Exception:
            pass

    # Compatibility input does not pretend to have extracted conclusions.
    # The synthesis model receives a bounded draft excerpt and must do the
    # cross-section judgement once, rather than spawning one repair call per
    # historical section.
    fallback = SectionHandoffCard(
        section_id=str(section.get("section_id") or ""),
        section_title=str(section.get("title") or ""),
        section_argument_completed=(
            section_dir / "SECTION_DRAFT_EN.md"
        ).exists(),
        used_paper_ids=paper_ids,
        used_chunk_ids=chunk_ids,
        visual_takeaways=[
            {
                "visual_chunk_id": visual_id,
                "argumentative_function": (
                    "A verified section visual available for article synthesis."
                ),
            }
            for visual_id in visual_ids
        ],
    )
    return fallback.model_dump(mode="json"), "legacy_draft_fallback"


def collect_article_synthesis_inputs(
    blueprint_path: Path,
    sections_root: Path,
    *,
    output_path: Path | None = None,
) -> Dict[str, Any]:
    """Collect bounded, traceable section memory for one synthesis call."""

    blueprint = _read_json(blueprint_path, {})
    if not isinstance(blueprint, dict) or not blueprint.get("sections"):
        raise ValueError("review blueprint is missing body sections")

    section_inputs: List[Dict[str, Any]] = []
    allowed_papers: Set[str] = set()
    allowed_chunks: Set[str] = set()
    allowed_visuals: Set[str] = set()
    fallback_sections: List[str] = []
    for section in blueprint.get("sections", []):
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("section_id") or "")
        section_dir = sections_root / section_id
        handoff, source = _load_handoff(section, section_dir)
        if source != "native":
            fallback_sections.append(section_id)
        paper_ids = list(handoff.get("used_paper_ids", []))
        chunk_ids = list(handoff.get("used_chunk_ids", []))
        visual_ids = [
            str(item.get("visual_chunk_id") or "")
            for item in handoff.get("visual_takeaways", [])
            if isinstance(item, dict)
        ]
        allowed_papers.update(paper_ids)
        allowed_chunks.update(chunk_ids)
        allowed_visuals.update(value for value in visual_ids if value)
        draft_path = section_dir / "SECTION_DRAFT_EN.md"
        draft = (
            draft_path.read_text(encoding="utf-8")
            if draft_path.exists()
            else ""
        )
        section_inputs.append(
            {
                "section_id": section_id,
                "section_title": str(section.get("title") or ""),
                "argument_role": str(section.get("argument_role") or ""),
                "chapter_argument": str(section.get("chapter_argument") or ""),
                "synthesis_task": str(section.get("synthesis_task") or ""),
                "handoff_source": source,
                "handoff_card": handoff,
                "bounded_draft_memory": _bounded_draft_memory(draft),
                "source_diversity": {
                    "unique_papers": len(set(paper_ids)),
                    "direct_papers": len(set(paper_ids)),
                },
            }
        )

    payload = {
        "schema_version": "article_synthesis_input.v1",
        "article_question": str(
            blueprint.get("input_context", {}).get("user_question")
            or blueprint.get(
                "article_rhetorical_contract",
                {},
            ).get("central_question")
            or ""
        ),
        "methodology_identity": str(
            blueprint.get("methodology_identity")
            or "critical_narrative_review"
        ),
        "review_thesis": str(blueprint.get("review_thesis") or ""),
        "full_review_argument": str(
            blueprint.get("full_review_argument") or ""
        ),
        "article_rhetorical_contract": blueprint.get(
            "article_rhetorical_contract",
            {},
        ),
        "sections": section_inputs,
        "verified_id_allowlist": {
            "section_ids": [
                item["section_id"] for item in section_inputs
            ],
            "paper_ids": sorted(allowed_papers),
            "chunk_ids": sorted(allowed_chunks),
            "visual_ids": sorted(allowed_visuals),
        },
        "fallback_sections": fallback_sections,
        "usage_rule": (
            "Handoff cards and draft memories guide synthesis but are not "
            "independent evidence. Scientific citations remain traceable to "
            "the verified paper and chunk identifiers."
        ),
    }
    fingerprint_source = dict(payload)
    fingerprint_source.pop("verified_id_allowlist", None)
    payload["input_fingerprint"] = hashlib.sha256(
        json.dumps(
            fingerprint_source,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if output_path is not None:
        atomic_write_json(output_path, payload)
    return payload


def sanitize_article_synthesis_map(
    raw: Dict[str, Any],
    synthesis_input: Dict[str, Any],
) -> Tuple[ArticleSynthesisMap, Dict[str, Any]]:
    """Remove fabricated identifiers and validate article-wide synthesis."""

    allowlist = synthesis_input.get("verified_id_allowlist", {})
    section_ids = set(allowlist.get("section_ids", []))
    paper_ids = set(allowlist.get("paper_ids", []))
    visual_ids = set(allowlist.get("visual_ids", []))
    cleaned = dict(raw)
    removed = {
        "section_ids": [],
        "paper_ids": [],
        "visual_ids": [],
        "challenge_ids": [],
    }

    contributions_by_id = {
        str(item.get("section_id") or ""): item
        for item in cleaned.get("section_contributions", [])
        if isinstance(item, dict)
    }
    source_by_id = {
        item["section_id"]: item
        for item in synthesis_input.get("sections", [])
        if isinstance(item, dict)
    }
    contributions = []
    for section_id in allowlist.get("section_ids", []):
        item = dict(contributions_by_id.get(section_id, {}))
        item["section_id"] = section_id
        item.setdefault(
            "argument_role",
            str(source_by_id.get(section_id, {}).get("argument_role") or ""),
        )
        item["source_diversity"] = dict(
            source_by_id.get(section_id, {}).get("source_diversity") or {}
        )
        contributions.append(item)
    for section_id in contributions_by_id:
        if section_id not in section_ids:
            removed["section_ids"].append(section_id)
    cleaned["section_contributions"] = contributions

    challenges = []
    for item in cleaned.get("challenge_candidates", []):
        if not isinstance(item, dict):
            continue
        item = dict(item)
        linked = []
        for section_id in item.get("linked_section_ids", []):
            if section_id in section_ids:
                linked.append(section_id)
            else:
                removed["section_ids"].append(str(section_id))
        item["linked_section_ids"] = list(dict.fromkeys(linked))
        challenges.append(item)
    cleaned["challenge_candidates"] = challenges
    valid_challenge_ids = {
        str(item.get("challenge_id") or "")
        for item in challenges
        if str(item.get("challenge_id") or "")
    }

    outlook = []
    for item in cleaned.get("outlook_candidates", []):
        if not isinstance(item, dict):
            continue
        item = dict(item)
        linked = []
        for challenge_id in item.get("linked_challenge_ids", []):
            if challenge_id in valid_challenge_ids:
                linked.append(challenge_id)
            else:
                removed["challenge_ids"].append(str(challenge_id))
        item["linked_challenge_ids"] = list(dict.fromkeys(linked))
        outlook.append(item)
    cleaned["outlook_candidates"] = outlook

    references = dict(cleaned.get("reference_inventory") or {})
    for key in (
        "unique_paper_ids",
        "landmark_paper_ids",
        "frontier_paper_ids",
    ):
        accepted = []
        for paper_id in references.get(key, []):
            if paper_id in paper_ids:
                accepted.append(paper_id)
            else:
                removed["paper_ids"].append(str(paper_id))
        references[key] = list(dict.fromkeys(accepted))
    # Unique paper inventory is deterministic and must not depend on model recall.
    references["unique_paper_ids"] = sorted(paper_ids)
    cleaned["reference_inventory"] = references

    visual_inventory = dict(cleaned.get("visual_inventory") or {})
    accepted_visuals = []
    for visual_id in visual_inventory.get("existing_visual_ids", []):
        if visual_id in visual_ids:
            accepted_visuals.append(visual_id)
        else:
            removed["visual_ids"].append(str(visual_id))
    visual_inventory["existing_visual_ids"] = sorted(
        set(accepted_visuals) | visual_ids
    )
    cleaned["visual_inventory"] = visual_inventory
    cleaned["article_question"] = str(
        synthesis_input.get("article_question") or ""
    )
    cleaned["review_thesis"] = str(
        synthesis_input.get("review_thesis") or ""
    )
    cleaned["methodology_identity"] = str(
        synthesis_input.get("methodology_identity")
        or "critical_narrative_review"
    )

    serialized = json.dumps(cleaned, ensure_ascii=False)
    if _CJK.search(serialized):
        raise ValueError("article synthesis map contains CJK text")
    result = ArticleSynthesisMap.model_validate(cleaned)
    if len(result.section_contributions) != len(section_ids):
        raise ValueError("article synthesis map does not cover every body section")
    audit = {
        "status": "passed",
        "removed_unverified_ids": {
            key: sorted(set(values)) for key, values in removed.items()
        },
        "section_count": len(result.section_contributions),
        "challenge_count": len(result.challenge_candidates),
        "outlook_count": len(result.outlook_candidates),
        "unique_paper_count": len(
            result.reference_inventory.unique_paper_ids
        ),
        "visual_count": len(result.visual_inventory.existing_visual_ids),
    }
    return result, audit
