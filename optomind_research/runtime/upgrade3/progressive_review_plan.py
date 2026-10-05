"""Progressive, tool-connected planning for evidence-led review writing.

The planner reads the complete B pool before proposing a scope, runs planning
tools during both planning levels, and produces a coordinated outline plus
self-contained chapter packets. All collaborators are injectable so the full
workflow can be exercised without model or network calls.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .chapter_arrangement import (
    distinct_tool_material_sources,
    merge_tool_supplement_entry,
    resolve_tool_materials,
    tool_supplement_entry,
)
from .module4.runtime import QwenTransportError


SCHEMA_VERSION = "optomind.progressive_review_plan.v1"
DEFAULT_PLANNER_MODEL = "qwen3.5-plus"
DEFAULT_READER_MODEL = "qwen3.7-flash"
DEFAULT_POOL_PATH = Path("outputs/planning_support/20260923/practical_refresh/supplement_live/PLANNING_POOL.jsonl")
DEFAULT_PLAN_PATH = Path("outputs/upgrade3/CROSSDOMAIN_REWORK/X1_microbiome_ICI/PLAN.json")
DEFAULT_BUDGET_LEDGER = Path("outputs/review_blueprint/20260922_phase1/budget.sqlite")
DEFAULT_TOKENIZER_PATH = Path("data/tokenizers/qwen3_5_9b/tokenizer.json")
MAX_INPUT_TOKENS = 991_808
TOKEN_MARGIN_MULTIPLIER = 1.12
TOKEN_FRAMING_MARGIN = 8_192
SOURCE_ROUTING_BATCH_SIZE = 60
SOURCE_ROUTING_WORKERS = 3
CASE_GROUP_ROUTE_BATCH_SIZE = 96

# This policy is owned by the local planner contract.  Model responses may
# describe editorial changes, but they cannot replace the citation policy used
# by writer packets and the final handoff.
CURRENT_CITATION_RULES = {
    "direct_source": "cite the supplied source_handle and bibliographic identity; the local packet maps it to paper_id",
    "review_reported_original": "keep the reporting review in source metadata; cite the resolved original study normally, with equal substantive use and no mandatory weaker wording or repeated review label",
    "scope": "preserve the actual reported findings and conditions; full-text access is not a prerequisite for using a sufficient abstract or expert review account",
}

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class ProgressivePlanError(ValueError):
    """Invalid planner inputs, response, or resume state."""


@dataclass(frozen=True)
class ProgressivePlannerConfig:
    topic_id: str
    pool_path: Path
    plan_path: Path
    output_dir: Path
    shared_deep_read_budget: int = 40
    chapter_workers: int = 4
    reader_workers: int = 4
    planner_model: str = DEFAULT_PLANNER_MODEL
    chapter_model: str = DEFAULT_READER_MODEL
    reader_model: str = DEFAULT_READER_MODEL
    timeout_seconds: float = 900.0
    thinking_budget: int = 8_192
    planner_output_tokens: int = 18_000
    chapter_output_tokens: int = 14_000
    tokenizer_path: Path = DEFAULT_TOKENIZER_PATH
    # M1 is deliberately opt-in.  The legacy planner keeps its existing
    # chapter payload and call sequence when this is false.
    planning_revision_enabled: bool = False
    local_material_index_path: Path | None = None
    planning_revision_candidate_limit: int = 12
    planning_revision_passages_per_paper: int = 2
    planning_revision_passage_chars: int = 1200
    # Optional, explicit one-shot recovery source.  This is never discovered
    # from history: callers name the exact prior output root to use.
    recovery_from: Path | None = None
    recovery_chapters: tuple[str, ...] = ()


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    return str(value)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProgressivePlanError(f"json_unreadable:{path.name}") from exc


def _safe_id(value: Any, fallback: str = "item") -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "")).strip("_-")
    return normalized[:80] or fallback


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _material_content_signature(value: Any) -> str:
    """Stable local signature for the material a downstream task consumes."""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


_MATERIAL_CONTENT_FIELDS = (
    "study_summary_A", "review_planning_B", "deep_read_material",
    "local_passages", "supplement_gap_material", "supplement_gap_materials",
    "supplement_material", "supplement_materials", "tool_materials", "tool_supplement_materials",
    "deep_read_materials", "local_passages_variants", "usable_content", "material",
    "material_identity_conflict",
)


def _material_content(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return only content a chapter role can use as evidence."""

    if not isinstance(value, Mapping):
        return {}
    return {key: value[key] for key in _MATERIAL_CONTENT_FIELDS if key in value and value[key] not in (None, "", [], {})}


def _saved_card_identity_conflict(source: Mapping[str, Any], card: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Legacy cards may omit identity; explicit contradictory identity is never borrowed."""
    identities = [card, *[card[key] for key in ("paper_identity", "record_identity")
                          if isinstance(card.get(key), Mapping)]]
    # A/B cards sometimes store their own paper identity in the direct A/B
    # container. Never recurse into examples/references describing other studies.
    for key in ("general_understanding", "review_planning"):
        container = card.get(key)
        if isinstance(container, Mapping):
            identities.extend(container[name] for name in ("paper_identity", "record_identity")
                              if isinstance(container.get(name), Mapping))
    return next((identity for identity in identities if _owner_identity_conflict(source, identity)), None)


def _refresh_local_material_snapshots(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Reload card/tool snapshots before case work, without trusting its text.

    Case grouping can point at a source, but only the local card or explicitly
    saved tool material is authoritative.  The caller keeps the pre-case
    records separately so this refresh can be compared as a real content delta.
    """

    refreshed = json.loads(json.dumps(records, ensure_ascii=False, default=_json_default))
    for record in refreshed:
        for source in record.get("source_materials") or []:
            if not isinstance(source, dict):
                continue
            path = Path(str(source.get("card_path") or ""))
            if not path.is_file():
                continue
            try:
                card = _read_json(path)
            except ProgressivePlanError:
                continue
            if not isinstance(card, Mapping):
                continue
            conflict = _saved_card_identity_conflict(source, card)
            if conflict is not None:
                # Quarantine only the incompatible card channel. Independently
                # sourced deep/supplement material remains available.
                source["study_summary_A"] = {}
                source["review_planning_B"] = {}
                source["material_identity_conflict"] = True
                source["material_identity_conflicts"] = [{
                    "channel": "saved_card", "reason": "source_identity_conflict",
                    "card_identity": {k: conflict.get(k) for k in ("paper_id", "canonical_paper_id", "doi", "title") if conflict.get(k)},
                }]
                for key in ("deep_read_material", "supplement_gap_material", "supplement_material", "material"):
                    independent = source.get(key)
                    if isinstance(independent, Mapping) and _saved_card_identity_conflict(source, independent) is not None:
                        source.pop(key, None)
                if _owner_material_has_content({**source, "material_identity_conflict": False}):
                    source.pop("material_identity_conflict", None)
                continue
            previously_conflicted = bool(source.get("material_identity_conflict") or source.get("material_identity_conflicts"))
            source.pop("material_identity_conflict", None)
            source.pop("material_identity_conflicts", None)
            if previously_conflicted:
                source["study_summary_A"] = {}
                source["review_planning_B"] = {}
            a = card.get("general_understanding")
            b = card.get("review_planning")
            if isinstance(a, Mapping) and a:
                source["study_summary_A"] = dict(a)
            if isinstance(b, Mapping) and b:
                source["review_planning_B"] = dict(b)
    return refreshed


_RECOVERY_COMPATIBILITY_FIELDS = (
    "topic_id", "research_question", "chapter", "chapter_plan",
    "candidate_materials", "candidate_navigation",
)


def _owner_cache_projection(value: Any, *, _scientific: bool = False) -> Any:
    """Compare owner-consumed values, excluding only known storage metadata.

    Scientific A/B content, conditions, task text and study dates remain exact.
    This is deliberately scoped to owner/recovery inputs, not every stage.
    """

    operational = {
        "card_path", "reading_path", "snapshot_path", "raw_response_path", "result_path",
        "output_path", "output_dir", "cache_path", "fetched_at", "retrieved_at",
        "saved_at", "generated_at", "created_at", "updated_at",
        "owner_material_resolution",
    }
    scientific = {
        "study_summary_A", "review_planning_B", "conditions", "finding", "findings",
        "content", "question_material", "paragraph_briefs", "required_outputs",
    }
    if isinstance(value, Mapping):
        return {
            key: _owner_cache_projection(child, _scientific=_scientific or key in scientific)
            for key, child in value.items()
            if _scientific or key not in operational
        }
    if isinstance(value, (list, tuple)):
        return [_owner_cache_projection(child, _scientific=_scientific) for child in value]
    return value


def _owner_success_record(path: Path) -> dict[str, Any] | None:
    """Read only validated, provenance-bearing successful owner records."""

    if not path.is_file():
        return None
    try:
        value = _read_json(path)
    except ProgressivePlanError:
        return None
    if (isinstance(value, Mapping) and value.get("status") == "complete"
            and value.get("owner_status") in {"updated", "no_change"}
            and isinstance(value.get("cache_inputs"), Mapping)
            and isinstance(value.get("updated_plan"), Mapping)):
        return dict(value)
    return None


def _recovery_outline_projection(outline: Any) -> Any:
    """Normalize accepted outline shapes without dropping global constraints."""

    if isinstance(outline, list):
        return {"chapters": _outline_chapter_rows(outline)}
    if isinstance(outline, Mapping):
        result = dict(outline)
        if "chapters" in result or "sections" in result:
            result.pop("sections", None)
            result["chapters"] = _outline_chapter_rows(outline)
        elif "shared_outline" in result:
            nested = _recovery_outline_projection(result.pop("shared_outline"))
            if not result:
                return nested
            result["shared_outline"] = nested
        return result
    return None


def _recovery_pool_material(handle: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """Build the current pool's authoritative A/B view for one handle."""

    card = _card_for_candidate(row)
    identity = card.get("paper_identity") if isinstance(card.get("paper_identity"), Mapping) else {}
    a = card.get("general_understanding") if isinstance(card.get("general_understanding"), Mapping) else {}
    b = card.get("review_planning") if isinstance(card.get("review_planning"), Mapping) else {}
    planning = row.get("planning_view") if isinstance(row.get("planning_view"), Mapping) else {}
    return {
        "source_handle": handle,
        "paper_id": _text(row.get("_paper_id") or _canonical_paper_id(row)),
        "card_path": _text(row.get("card_path")),
        "title": _text(identity.get("title") or row.get("title")),
        "doi": _text(identity.get("doi") or row.get("doi")),
        "year": _text(identity.get("year") or row.get("year")),
        "material_depth": _text((row.get("_b_summary") or {}).get("declared_content_depth")),
        "study_summary_A": dict(a),
        "review_planning_B": dict(b),
        "supplement_gap_material": row.get("supplement_gap_material") or {},
        "supplement_gap_materials": row.get("supplement_gap_materials") or [],
        "deep_read_material": row.get("deep_read_material") or {},
        "local_passages": row.get("local_passages") or {},
        "planning_view": dict(planning),
    }


def _refresh_owner_packet_materials(
    packet: dict[str, Any],
    *,
    pool_rows: Sequence[Mapping[str, Any]],
    deep_material_by_paper: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Refresh only existing material rows, including duplicated candidate views."""

    for key in ("source_materials", "candidate_materials"):
        if key not in packet:
            continue
        packet[key], _ = _resolve_owner_source_materials(
            source_materials=packet.get(key) or [], chapter_plan={}, pool_rows=pool_rows,
            deep_material_by_paper=deep_material_by_paper,
        )
    navigation = packet.get("candidate_navigation")
    if isinstance(navigation, Mapping) and "candidate_materials" in navigation:
        navigation = dict(navigation)
        navigation["candidate_materials"], _ = _resolve_owner_source_materials(
            source_materials=navigation.get("candidate_materials") or [], chapter_plan={}, pool_rows=pool_rows,
            deep_material_by_paper=deep_material_by_paper,
        )
        packet["candidate_navigation"] = navigation


def _owner_consumed_material(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize supplement aliases while retaining conflicting consumed text."""

    content = _material_content(value)
    if "deep_read_material" in content:
        content["deep_read_material"] = _compact_reading_material(content["deep_read_material"])
    for canonical, alias in (("supplement_gap_material", "supplement_material"),
                              ("supplement_gap_materials", "supplement_materials")):
        if value.get(alias) in (None, "", [], {}):
            continue
        if canonical not in content:
            content[canonical] = value[alias]
        elif _owner_cache_projection(content[canonical]) != _owner_cache_projection(value[alias]):
            content[alias] = value[alias]
    return content


def _recovery_material_matches(source: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """Require the same identity and consumed content before importing a row."""

    for key in ("source_handle", "paper_id", "doi", "title", "year"):
        source_value = _text(source.get(key))
        current_value = _text(current.get(key))
        if source_value and current_value and source_value != current_value:
            return False
    return _owner_cache_projection(_owner_consumed_material(source)) == _owner_cache_projection(_owner_consumed_material(current))


def _strip_recovery_case_suggestions(value: Any) -> Any:
    """Keep the successful plan's scientific content without pending proposals."""

    if isinstance(value, Mapping):
        return {
            key: _strip_recovery_case_suggestions(child)
            for key, child in value.items()
            if key != "case_suggestions"
        }
    if isinstance(value, list):
        return [_strip_recovery_case_suggestions(child) for child in value]
    return value


def _recovery_compare_value(key: str, value: Any) -> Any:
    """Ignore historical pending proposals and material-list bookkeeping."""

    normalized = _strip_recovery_case_suggestions(value)
    if key == "chapter" and isinstance(normalized, Mapping):
        normalized = {
            name: child for name, child in normalized.items()
            if name not in {"source_ids", "source_handles", "excluded_source_ids", "excluded_source_handles", "source_exclusion_notes"}
        }
    return _owner_cache_projection(normalized)


def recover_compatible_chapter_details(
    records: Sequence[Mapping[str, Any]],
    *,
    recovery_root: str | Path,
    current_pool: Sequence[Mapping[str, Any]] = (),
    shared_outline: Any = None,
    output_root: str | Path | None = None,
    chapter_ids: Sequence[str] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Seed the current in-memory baseline from explicitly named good revisions.

    The source revision's original ``cache_inputs`` are compared with the
    current chapter packet.  Only the source handles used by its updated plan
    are considered.  A missing current row may be restored from the source
    cache only after the current pool proves the same identity and consumed
    material; the restored row comes from current inputs, never stale content.
    The full historical pool and pending case suggestions are never copied.
    """

    source_root = Path(recovery_root).resolve()
    selected = {_text(item) for item in chapter_ids if _text(item)}
    pool_by_handle = {
        _text(row.get("_source_handle")): row
        for row in current_pool
        if isinstance(row, Mapping) and _text(row.get("_source_handle"))
    }
    current_root = Path(output_root).resolve() if output_root else None
    result: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "status": "complete",
        "source_root": str(source_root),
        "chapters": {},
    }
    for original in records:
        packet = json.loads(json.dumps(original, ensure_ascii=False, default=_json_default))
        chapter = packet.get("chapter") if isinstance(packet.get("chapter"), Mapping) else {}
        chapter_id = _text(chapter.get("chapter_id"))
        if selected and chapter_id not in selected:
            result.append(packet)
            continue
        # Existing packet snapshots cannot overrule explicitly corrected pool
        # material even when every historical required handle is already there.
        _refresh_owner_packet_materials(packet, pool_rows=current_pool)
        entry: dict[str, Any] = {"status": "skipped", "chapter_id": chapter_id}
        revision_root = source_root / "stages" / "affected_chapter_revision"
        revision_path = revision_root / f"{_safe_id(chapter_id)}.json"
        success_path = revision_root / "successful" / revision_path.name
        revision = _owner_success_record(revision_path) if chapter_id else None
        if revision is None and chapter_id:
            revision = _owner_success_record(success_path)
            if revision is not None:
                revision_path = success_path
        if revision is None:
            entry["reason"] = ("source_revision_not_successful" if revision_path.is_file() or success_path.is_file()
                               else "source_successful_revision_missing")
            report["chapters"][chapter_id or "unknown"] = entry
            result.append(packet)
            continue
        cache = revision["cache_inputs"]
        updated = revision["updated_plan"]
        # First reconstruct the same compatible seed. A current owner success
        # is replayed by its own input-aware cache below, never by merely
        # skipping recovery and leaving the coarse chapter plan in its place.
        mismatch = []
        if any(_text(value) and _text(value) != chapter_id for value in
               (revision.get("chapter_id"), updated.get("chapter_id"))):
            mismatch.append("chapter_ownership")
        for key in _RECOVERY_COMPATIBILITY_FIELDS:
            if _recovery_compare_value(key, cache.get(key)) != _recovery_compare_value(key, packet.get(key)):
                mismatch.append(key)
        source_chapter_path = source_root / "stages" / "chapters" / f"{_safe_id(chapter_id)}.json"
        source_packet: Mapping[str, Any] = {}
        if shared_outline is None or not source_chapter_path.is_file():
            mismatch.append("shared_outline_unavailable")
        else:
            try:
                loaded_source_packet = _read_json(source_chapter_path)
                source_packet = loaded_source_packet if isinstance(loaded_source_packet, Mapping) else {}
            except ProgressivePlanError:
                source_packet = {}
            source_outline = _recovery_outline_projection(source_packet.get("shared_outline"))
            current_outline = _recovery_outline_projection(packet.get("shared_outline"))
            expected_outline = _recovery_outline_projection(shared_outline)
            if source_outline is None or current_outline is None or expected_outline is None:
                mismatch.append("shared_outline_unavailable")
            elif source_outline != current_outline or current_outline != expected_outline:
                mismatch.append("shared_outline")
            outline_ids = {_text(row.get("chapter_id") or row.get("id"))
                           for row in _outline_chapter_rows(shared_outline)}
            if outline_ids and chapter_id not in outline_ids:
                mismatch.append("chapter_ownership")
        if mismatch:
            entry["reason"] = "compatibility_mismatch"
            entry["fields"] = mismatch
            report["chapters"][chapter_id] = entry
            result.append(packet)
            continue
        source_materials = {
            _text(row.get("source_handle")): dict(row)
            for row in cache.get("source_materials") or []
            if isinstance(row, Mapping) and _text(row.get("source_handle"))
        }
        # Some successful revision caches carried a compact material subset.
        # The source chapter detail is the same-run original baseline for any
        # required handle omitted from that compact cache; it is still checked
        # against the current packet before the historical row is used.
        for row in source_packet.get("source_materials") or []:
            if isinstance(row, Mapping) and _text(row.get("source_handle")):
                source_materials.setdefault(_text(row.get("source_handle")), dict(row))
        current_materials = {
            _text(row.get("source_handle")): dict(row)
            for row in packet.get("source_materials") or []
            if isinstance(row, Mapping) and _text(row.get("source_handle"))
        }
        recovered_plan = _strip_recovery_case_suggestions(updated)
        required_handles, required_papers = _source_keys_in_value(recovered_plan)
        source_by_paper = {
            _text(row.get("paper_id")): handle
            for handle, row in source_materials.items()
            if _text(row.get("paper_id"))
        }
        by_paper = {
            _text(row.get("paper_id")): handle
            for handle, row in current_materials.items()
            if _text(row.get("paper_id"))
        }
        unresolved_papers = sorted(paper for paper in required_papers if paper not in source_by_paper)
        required_handles.update(source_by_paper[paper] for paper in required_papers if paper in source_by_paper)
        required_handles.update(by_paper[paper] for paper in required_papers if paper in by_paper)
        if unresolved_papers:
            entry["reason"] = "required_material_unavailable"
            entry["paper_ids"] = unresolved_papers
            report["chapters"][chapter_id] = entry
            result.append(packet)
            continue
        material_mismatch = None
        for handle in sorted(required_handles):
            source_row = source_materials.get(handle)
            current_row = current_materials.get(handle)
            if current_row is None and source_row is not None:
                current_handle = by_paper.get(_text(source_row.get("paper_id")))
                current_row = current_materials.get(current_handle) if current_handle else None
            if source_row is None:
                material_mismatch = ("required_material_unavailable", handle)
                break
            if current_row is not None and not _recovery_material_matches(source_row, current_row):
                material_mismatch = ("required_material_identity_or_content_mismatch", handle)
                break
        if material_mismatch:
            entry["reason"], entry["handle"] = material_mismatch
            report["chapters"][chapter_id] = entry
            result.append(packet)
            continue
        missing = sorted(handle for handle in required_handles if handle not in current_materials)
        restored: list[str] = []
        for handle in missing:
            source_row = source_materials.get(handle)
            pool_row = pool_by_handle.get(handle)
            if source_row is None or pool_row is None:
                entry["reason"] = "required_material_unavailable"
                entry["handles"] = missing
                report["chapters"][chapter_id] = entry
                result.append(packet)
                break
            current_row = _recovery_pool_material(handle, pool_row)
            if not _recovery_material_matches(source_row, current_row):
                entry["reason"] = "required_material_identity_or_content_mismatch"
                entry["handle"] = handle
                report["chapters"][chapter_id] = entry
                result.append(packet)
                break
            current_materials[handle] = current_row
            restored.append(handle)
        else:
            packet["source_materials"] = [
                current_materials.get(_text(row.get("source_handle")), dict(row))
                for row in packet.get("source_materials") or []
                if isinstance(row, Mapping)
            ]
            packet["source_materials"].extend(current_materials[handle] for handle in restored)
            packet["chapter_plan"] = recovered_plan
            packet["chapter_recovery"] = {
                "source_root": str(source_root),
                "source_revision": str(revision_path),
                "restored_materials": restored,
                "owner_status": revision.get("owner_status"),
            }
            entry.update({"status": "recovered", "restored_materials": restored,
                          "paragraph_briefs": sum(len(unit.get("paragraph_briefs") or []) for unit in updated.get("units") or [] if isinstance(unit, Mapping))})
            report["chapters"][chapter_id] = entry
            if current_root is not None:
                _atomic_json(current_root / "stages" / "recovered_chapters" / f"{_safe_id(chapter_id)}.json", packet)
            result.append(packet)
    recovered = [item for item in report["chapters"].values() if item.get("status") == "recovered"]
    report["recovered_chapters"] = len(recovered)
    return result, report


def load_original_plan(path: str | Path) -> dict[str, Any]:
    raw = _read_json(Path(path))
    if not isinstance(raw, Mapping):
        raise ProgressivePlanError("plan_must_be_object")
    if isinstance(raw.get("plan"), Mapping):
        if str(raw.get("status") or "").casefold() != "ok":
            raise ProgressivePlanError("plan_wrapper_status_not_ok")
        plan = dict(raw["plan"])
    else:
        plan = dict(raw)
    if not _text(plan.get("question_en") or plan.get("question")):
        raise ProgressivePlanError("plan_question_missing")
    if not isinstance(plan.get("facets"), list) or not plan["facets"]:
        raise ProgressivePlanError("plan_facets_missing")
    return plan


def _canonical_paper_id(row: Mapping[str, Any]) -> str:
    planning = row.get("planning_view") if isinstance(row.get("planning_view"), Mapping) else {}
    identity = planning.get("paper_identity") if isinstance(planning.get("paper_identity"), Mapping) else {}
    return _text(identity.get("canonical_paper_id") or row.get("paper_id") or row.get("canonical_paper_id"))


def _compact_b_record(row: Mapping[str, Any]) -> dict[str, Any]:
    planning = row.get("planning_view") if isinstance(row.get("planning_view"), Mapping) else {}
    identity = planning.get("paper_identity") if isinstance(planning.get("paper_identity"), Mapping) else {}
    return {
        "source_handle": _text(row.get("_source_handle")),
        "title": _text(identity.get("title") or row.get("title")),
        "doi": _text(identity.get("doi") or row.get("doi")),
        "year": _text(identity.get("year") or row.get("year")),
        "material_scope": _text(planning.get("material_scope") or row.get("material_scope")),
        "declared_content_depth": _text(planning.get("declared_content_depth") or row.get("material_depth_label")),
        "planning_summary": planning.get("planning_summary") or "",
        "facet_contributions": planning.get("facet_contributions") or [],
        "scope_interpretation_cautions": planning.get("scope_interpretation_cautions") or [],
        "broader_review_uses": planning.get("broader_review_uses") or [],
        "root_review_note": dict(row.get("root_review_note") or {}),
        "supplement_material_status": _text(row.get("supplement_material_status") or row.get("supplement_status")),
        "supplement_gap_material": row.get("supplement_gap_material") or {},
    }



def _compact_original_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Carry the user's intent, without the search execution/corpus payload."""
    result = {key: plan[key] for key in (
        "question", "question_en", "research_object", "scope", "shared_scope",
        "criteria", "additional_constraints", "ambiguity",
    ) if key in plan}
    result["facets"] = [
        {key: facet[key] for key in ("id", "facet_id", "ask", "question", "scope", "filters", "must_exclude")
         if key in facet}
        for facet in plan.get("facets") or [] if isinstance(facet, Mapping)
    ]
    return result


def _review_guidance(
    level1_outline: Mapping[str, Any], harmonized: Mapping[str, Any],
    improvement: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep scope and argument distinct, including legacy missing arguments."""
    improvement = improvement or {}
    scope = next((value for value in (
        improvement.get("finalized_shared_scope"), improvement.get("final_shared_scope"),
        harmonized.get("shared_scope"), level1_outline.get("shared_scope"),
    ) if isinstance(value, Mapping) and value or isinstance(value, str) and value.strip()), {})
    result = {"shared_scope": dict(scope) if isinstance(scope, Mapping) else {"statement": scope.strip()},
              "review_argument": "", "review_argument_status": "missing", "review_argument_source": "missing"}
    for source, value, status in (
        ("whole_plan_improvement", improvement.get("finalized_review_argument"), "calibrated"),
        ("whole_plan_improvement", improvement.get("review_argument"), "calibrated"),
        ("harmonized_scope", harmonized.get("review_argument"), "carried_forward_uncalibrated"),
        ("level1_outline", level1_outline.get("review_argument"), "carried_forward_uncalibrated"),
    ):
        if isinstance(value, Mapping) and value or isinstance(value, str) and value.strip():
            argument = json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default) if isinstance(value, Mapping) else value.strip()
            result.update(review_argument=argument, review_argument_status=status, review_argument_source=source)
            break
    return result


def _material_theme_snapshot(
    pool_rows: Sequence[Mapping[str, Any]], deep_materials: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Snapshot actual planning content, excluding paths and routing metadata."""
    output = {}
    for row in pool_rows:
        paper_id = _text(row.get("_paper_id") or _canonical_paper_id(row))
        b = row.get("_b_summary") or _compact_b_record(row)
        content = {"review_planning_B": {key: b[key] for key in (
            "planning_summary", "facet_contributions", "scope_interpretation_cautions", "broader_review_uses",
        ) if b.get(key)}, **_material_content(row)}
        deep = (deep_materials or {}).get(paper_id)
        if deep:
            content["deep_read_material"] = _compact_reading_material(deep)
        output[paper_id] = _owner_cache_projection(content)
    return output


def _refresh_material_theme_inventory(
    inventory: Sequence[Any], baseline: Mapping[str, Any], pool_rows: Sequence[Mapping[str, Any]],
    deep_materials: Mapping[str, Any] | None = None,
) -> list[Any]:
    """Append compact, attributed deltas to the existing semantic inventory.

    This is a navigation update, not another planner or a replacement for the
    complete evidence delivered to the affected chapter owner.
    """
    updated = [dict(item) if isinstance(item, Mapping) else item for item in inventory
               if not (isinstance(item, Mapping) and item.get("inventory_origin") == "material_update")]
    current = _material_theme_snapshot(pool_rows, deep_materials)
    for row in pool_rows:
        paper_id = _text(row.get("_paper_id") or _canonical_paper_id(row))
        material = current.get(paper_id) or {}
        if material == baseline.get(paper_id) or not _owner_material_has_content(material):
            continue
        excerpt = json.dumps(material, ensure_ascii=False, sort_keys=True, default=_json_default)
        updated.append({
            "inventory_origin": "material_update", "source_handle": _text(row.get("_source_handle")),
            "paper_id": paper_id, "change": "updated" if paper_id in baseline else "new",
            "material_excerpt": excerpt[:1800], "excerpt_truncated": len(excerpt) > 1800,
            "content_signature": _material_content_signature(material),
        })
    return updated


def _attach_late_route_materials(
    records: Sequence[Mapping[str, Any]], routing: Mapping[str, Any], pool_rows: Sequence[Mapping[str, Any]],
    deep_materials: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Give newly routed material to its owner before final coordination."""
    routes = [row for row in routing.get("late_source_routes") or []
              if isinstance(row, Mapping) and row.get("route_status") == "assigned"]
    materials = {row["source_handle"]: row for row in _case_selection_material_rows(
        [_text(row.get("source_handle")) for row in routes], records, pool_rows, deep_materials)}
    output = json.loads(json.dumps(records, ensure_ascii=False, default=_json_default))
    for packet in output:
        chapter = packet.get("chapter") or {}
        chapter_id = _text(chapter.get("chapter_id"))
        sources = list(packet.get("source_materials") or [])
        existing = {_text(row.get("source_handle")) for row in sources if isinstance(row, Mapping)}
        for route in routes:
            handle = _text(route.get("source_handle"))
            material = materials.get(handle) or {}
            if chapter_id not in (route.get("chapter_ids") or []) or handle in existing or not _owner_material_has_content(material):
                continue
            sources.append(dict(material))
            existing.add(handle)
            for key, value in (("source_handles", handle), ("source_ids", _text(material.get("paper_id")))):
                if value:
                    chapter[key] = list(dict.fromkeys([*_source_values(chapter.get(key)), value]))
        packet["source_materials"] = sources
    return output

def _chapter_material_changes(
    detail_records: Sequence[Mapping[str, Any]], baseline_detail_records: Sequence[Mapping[str, Any]] | None,
    case_response: Mapping[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Compare actual consumed content, preserving the existing late detector."""
    case_response = case_response or {}
    by_chapter_material: dict[str, list[dict[str, Any]]] = {}
    source_materials_by_chapter: dict[str, dict[str, dict[str, Any]]] = {}
    for record in detail_records:
        chapter = record.get("chapter") if isinstance(record.get("chapter"), Mapping) else {}
        chapter_id = _text(chapter.get("chapter_id"))
        if not chapter_id:
            continue
        source_materials_by_chapter[chapter_id] = {
            _text(item.get("source_handle")): dict(item)
            for item in (record.get("source_materials") or [])
            if isinstance(item, Mapping) and _text(item.get("source_handle"))
        }
    baseline_materials_by_chapter: dict[str, dict[str, dict[str, Any]]] = {}
    for record in (baseline_detail_records or detail_records):
        chapter = record.get("chapter") if isinstance(record.get("chapter"), Mapping) else {}
        chapter_id = _text(chapter.get("chapter_id"))
        if not chapter_id:
            continue
        baseline_materials_by_chapter[chapter_id] = {
            _text(item.get("source_handle")): dict(item)
            for item in (record.get("source_materials") or [])
            if isinstance(item, Mapping) and _text(item.get("source_handle"))
        }
    # Compare actual material regardless of whether the case editor chose
    # to mention it. Its labels and repeated contribution text are not a
    # gate on a chapter owner's access to changed content.
    case_notes: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for addition in case_response.get("additions") or []:
        if not isinstance(addition, Mapping):
            continue
        unit_key = _text(addition.get("unit_key"))
        chapter_id = unit_key.split(":", 1)[0] if ":" in unit_key else _text(addition.get("chapter_id"))
        for study in addition.get("studies") or []:
            if isinstance(study, Mapping):
                case_notes.setdefault((chapter_id, _text(study.get("source_handle"))), []).append(dict(study))
    for chapter_id, current_sources in source_materials_by_chapter.items():
        prior_sources = baseline_materials_by_chapter.get(chapter_id, {})
        for handle, current_source in current_sources.items():
            current_content = _owner_consumed_material(current_source)
            prior_content = _owner_consumed_material(prior_sources.get(handle) or {})
            if _owner_cache_projection(current_content) == _owner_cache_projection(prior_content):
                continue
            by_chapter_material.setdefault(chapter_id, []).append({
                "source_handle": handle,
                "studies": case_notes.get((chapter_id, handle), []),
                "source_materials": [dict(current_source)],
                "previous_material": prior_content,
                "content_signature": _material_content_signature(current_content),
            })

    return by_chapter_material


def load_planning_pool(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        with source.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ProgressivePlanError(f"pool_jsonl_invalid:{line_number}") from exc
                if not isinstance(raw, Mapping):
                    raise ProgressivePlanError(f"pool_row_not_object:{line_number}")
                row = dict(raw)
                paper_id = _canonical_paper_id(row)
                if not paper_id:
                    raise ProgressivePlanError(f"pool_paper_id_missing:{line_number}")
                if paper_id in seen:
                    raise ProgressivePlanError(f"pool_duplicate_paper_id:{paper_id}")
                seen.add(paper_id)
                row["_paper_id"] = paper_id
                row["_source_handle"] = f"P{len(rows) + 1:04d}"
                row["_b_summary"] = _compact_b_record(row)
                rows.append(row)
    except OSError as exc:
        raise ProgressivePlanError("pool_unreadable") from exc
    if not rows:
        raise ProgressivePlanError("pool_empty")
    return rows


def _refresh_source_handles(rows: list[dict[str, Any]]) -> dict[str, str]:
    handle_to_id: dict[str, str] = {}
    for index, row in enumerate(rows, start=1):
        paper_id = _canonical_paper_id(row)
        if not paper_id:
            continue
        row["_paper_id"] = paper_id
        handle = _text(row.get("_source_handle")) or f"P{index:04d}"
        row["_source_handle"] = handle
        row["_b_summary"] = _compact_b_record(row)
        handle_to_id[handle] = paper_id
    return handle_to_id


def _resolve_source_handle(value: Any, handle_to_id: Mapping[str, str]) -> str:
    if isinstance(value, Mapping):
        value = value.get("source_handle") or value.get("paper_id") or value.get("canonical_paper_id")
    normalized = _text(value)
    return str(handle_to_id.get(normalized, normalized))


def _source_values(value: Any) -> list[str]:
    if isinstance(value, (str, Mapping)):
        value = [value]
    return list(dict.fromkeys(handle for item in (value or []) if (handle := _resolve_source_handle(item, {}))))


def _resolve_planner_handles(value: Mapping[str, Any], handle_to_id: Mapping[str, str]) -> dict[str, Any]:
    """Translate short prompt handles back to canonical IDs at the local edge."""

    result = json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))
    for key in ("chapter_proposals", "chapters", "harmonized_chapters"):
        for row in result.get(key) or []:
            if not isinstance(row, Mapping):
                continue
            source_values = _source_values(row.get("source_handles") or row.get("source_ids") or row.get("paper_ids"))
            row["source_ids"] = list(dict.fromkeys(_resolve_source_handle(item, handle_to_id) for item in source_values if _text(item)))
            excluded = row.get("excluded_source_handles") or row.get("excluded_source_ids") or []
            if isinstance(excluded, str):
                excluded = [excluded]
            if excluded:
                if any(isinstance(item, Mapping) for item in excluded):
                    row["source_exclusion_notes"] = excluded
                row["excluded_source_handles"] = _source_values(excluded)
                row["excluded_source_ids"] = list(dict.fromkeys(_resolve_source_handle(item, handle_to_id) for item in excluded if _text(item)))
            if row.get("directed_reads") or row.get("supplement_requests"):
                row.update(_resolve_planner_handles(row, handle_to_id))
    for key in ("directed_reads",):
        for row in result.get(key) or []:
            if isinstance(row, Mapping):
                raw_id = (row.get("source_handle") or row.get("paper_id")
                          or row.get("canonical_paper_id") or row.get("handle"))
                row["paper_id"] = _resolve_source_handle(raw_id, handle_to_id)
    for row in result.get("supplement_requests") or []:
        if not isinstance(row, Mapping):
            continue
        row["known_papers"] = [dict(item) if isinstance(item, Mapping) else {"paper_id": _resolve_source_handle(item, handle_to_id)}
                               for item in row.get("known_papers") or []]
        for candidate in row.get("known_papers") or []:
            if isinstance(candidate, Mapping):
                raw_id = candidate.get("canonical_paper_id") or candidate.get("paper_id") or candidate.get("source_handle")
                candidate["paper_id"] = _resolve_source_handle(raw_id, handle_to_id)
    return result


def _planner_scope_copy(value: Mapping[str, Any], paper_to_handle: Mapping[str, str]) -> dict[str, Any]:
    """Send editorial content; full route inventories stay in the local pool."""
    result = json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))
    for key in ("chapter_proposals", "chapters", "harmonized_chapters"):
        for row in result.get(key) or []:
            if not isinstance(row, Mapping):
                continue
            ids = row.get("source_ids") or row.get("paper_ids") or []
            handles = row.get("source_handles") or [paper_to_handle.get(_text(item), _text(item)) for item in ids]
            row["available_source_count"] = len(set(_source_values(handles)))
            row.pop("source_handles", None)
            excluded = row.get("excluded_source_ids") or row.get("excluded_source_handles") or []
            if isinstance(excluded, str):
                excluded = [excluded]
            if excluded:
                row["excluded_source_handles"] = list(dict.fromkeys(paper_to_handle.get(_text(item), _text(item)) for item in excluded if _text(item)))
            row.pop("source_ids", None)
            row.pop("paper_ids", None)
            row.pop("excluded_source_ids", None)
    return result


def _outline_chapter_rows(outline: Any) -> list[dict[str, Any]]:
    """Read ordinary or mildly nested ordered outline shapes."""
    if isinstance(outline, Mapping):
        candidates = outline.get("chapters") or outline.get("sections") or []
        if not candidates and isinstance(outline.get("shared_outline"), (Mapping, list)):
            return _outline_chapter_rows(outline.get("shared_outline"))
    elif isinstance(outline, list):
        candidates = outline
    else:
        candidates = []
    output: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        if isinstance(item.get("chapter_proposals"), Mapping):
            nested = dict(item["chapter_proposals"])
            nested.setdefault("chapter_id", item.get("chapter_id") or item.get("id"))
            output.append(nested)
        elif isinstance(item.get("chapter_proposals"), list):
            output.extend(dict(row) for row in item["chapter_proposals"] if isinstance(row, Mapping))
        else:
            output.append(dict(item))
    return output


def _compact_routing_outline(outline: Any) -> list[dict[str, str]]:
    return [
        {
            "chapter_id": _text(row.get("chapter_id") or row.get("id")),
            "title": _text(row.get("title") or row.get("chapter_title")),
            "focus": _text(row.get("focus") or row.get("purpose") or row.get("scope") or row.get("question")),
        }
        for row in _outline_chapter_rows(outline)
        if _text(row.get("chapter_id") or row.get("id"))
    ]


def _normalize_proposal_response(response: Mapping[str, Any]) -> dict[str, Any]:
    """Accept the expected chapter list and the observed nested outline wrapper."""
    output = json.loads(json.dumps(response, ensure_ascii=False, default=_json_default))
    rows = output.get("chapter_proposals")
    if isinstance(rows, Mapping):
        rows = [dict(rows)]
    if not isinstance(rows, list):
        rows = []
    if not rows:
        outline = output.get("shared_outline")
        rows = _outline_chapter_rows(outline)
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        nested = row.get("chapter_proposals")
        if isinstance(nested, Mapping):
            item = {**dict(nested), **{key: value for key, value in row.items() if key != "chapter_proposals"}}
        else:
            item = dict(row)
        nested_rows = item.get("proposals")
        if nested_rows and not _text(item.get("chapter_id")):
            normalized.extend(dict(child) for child in nested_rows if isinstance(child, Mapping))
        else:
            normalized.append(item)
    output["chapter_proposals"] = normalized
    return output


def _merge_supplement_pool_updates(pool_rows: list[dict[str, Any]], tool_result: Mapping[str, Any], *, preserve_current_material: bool = False) -> list[dict[str, Any]]:
    """Add supplement discoveries and merge their upgraded planning material into B."""

    by_id = {str(row.get("_paper_id")): row for row in pool_rows}
    changed = False
    groups: list[Mapping[str, Any]] = []
    pending: list[Any] = list(tool_result.get("supplement_results") or [])
    pending.extend(tool_result.get("results") or [])
    while pending:
        group = pending.pop(0)
        if not isinstance(group, Mapping):
            continue
        groups.append(group)
        pending.extend(group.get("results") or [])
    candidate_rows: list[Mapping[str, Any]] = []
    for group in groups:
        candidate_rows.extend(row for row in (group.get("candidate_rows") or []) if isinstance(row, Mapping))
        derived_path = Path(str(group.get("derived_pool_path") or ""))
        if derived_path.is_file():
            try:
                with derived_path.open("r", encoding="utf-8-sig") as handle:
                    candidate_rows.extend(row for line in handle if line.strip() for row in [json.loads(line)] if isinstance(row, Mapping))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
    for raw in candidate_rows:
        row = dict(raw)
        paper_id = _canonical_paper_id(row)
        if not paper_id:
            continue
        if paper_id in by_id:
            current = by_id[paper_id]
            incoming_unit = _text(row.get("supplement_source_unit_id"))
            current_unit = _text(current.get("supplement_source_unit_id"))
            incoming_version = int(row.get("supplement_version") or 0)
            current_version = int(current.get("supplement_version") or 0)
            # Each supplement's derived pool also contains unchanged base rows.
            # Do not let a later gap's copy of the base card replace an upgraded
            # card that was returned by an earlier gap.
            incoming_is_upgrade = bool(
                (incoming_unit and (not current_unit or incoming_version > current_version))
                or (isinstance(row.get("supplement_gap_material"), Mapping) and row.get("supplement_gap_material") and not current_unit)
            )
            if incoming_is_upgrade:
                for key in ("card_path", "planning_view", "material_depth_label", "supplement_version", "prior_card_path", "prior_source_unit_ids", "prior_supplement_gap_materials"):
                    if key in row and (not preserve_current_material or key not in {"card_path", "planning_view", "material_depth_label"}):
                        current[key] = row[key]
                if incoming_unit:
                    current["supplement_source_unit_id"] = incoming_unit
            material = row.get("supplement_gap_material")
            prior = current.get("supplement_gap_material")
            # A gap label is an owner/routing label, not a material identity.
            # Complementary changed-output and retry material must coexist;
            # stale derived base rows may add history but cannot reset active B.
            material_rows = [*(current.get("supplement_gap_materials") or []),
                             *(current.get("prior_supplement_gap_materials") or []), prior,
                             *(row.get("supplement_gap_materials") or []),
                             *(row.get("prior_supplement_gap_materials") or []), material]
            distinct = {_material_content_signature(item): dict(item) for item in material_rows
                        if isinstance(item, Mapping) and item}
            if distinct:
                current["supplement_gap_materials"] = list(distinct.values())
            if isinstance(material, Mapping) and material and ((incoming_is_upgrade and not preserve_current_material) or not prior):
                current["supplement_gap_material"] = dict(material)
                current["supplement_material_status"] = row.get("supplement_material_status") or row.get("supplement_status") or "ready"
            if not current_unit and incoming_unit:
                current["supplement_source_unit_id"] = incoming_unit
        else:
            row["_paper_id"] = paper_id
            row["_source_handle"] = ""
            row["_b_summary"] = {}
            history = [*(row.get("supplement_gap_materials") or []),
                       *(row.get("prior_supplement_gap_materials") or []), row.get("supplement_gap_material")]
            distinct = {_material_content_signature(item): dict(item) for item in history if isinstance(item, Mapping) and item}
            if distinct:
                row["supplement_gap_materials"] = list(distinct.values())
            pool_rows.append(row)
            by_id[paper_id] = row
        changed = True
    if changed:
        _refresh_source_handles(pool_rows)
    return pool_rows


def qwen_local_token_counter(tokenizer_path: str | Path = DEFAULT_TOKENIZER_PATH) -> Callable[[bytes, Sequence[Mapping[str, Any]]], int]:
    """Return a local Qwen-family tokenizer counter without downloading assets."""

    path = Path(tokenizer_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.is_file():
        raise ProgressivePlanError(f"local_tokenizer_missing:{path}")
    try:
        from tokenizers import Tokenizer
    except ImportError as exc:
        raise ProgressivePlanError("tokenizers_package_missing") from exc

    local = threading.local()

    def count(_request_bytes: bytes, messages: Sequence[Mapping[str, Any]]) -> int:
        tokenizer = getattr(local, "tokenizer", None)
        if tokenizer is None:
            tokenizer = Tokenizer.from_file(str(path))
            local.tokenizer = tokenizer
        text = "\n".join(
            f"{_text(message.get('role'))}\n{_text(message.get('content'))}"
            for message in messages
            if isinstance(message, Mapping)
        )
        return len(tokenizer.encode(text).ids)

    return count


def _planner_instructions(stage: str, *, planning_revision: bool = False) -> str:
    common = (
        "你是学术综述规划编辑。目标是形成有论证主线的长篇综述，规划内容用中文撰写，论文原题和专名可保留原文；不要写成问答清单。"
        "\n\n本规划链只规划综述正文中需要实质展开的知识与分析。全篇开场、向读者宣告综述范围与文章组织、摘要，以及收束全文的最终总结，由正文完成后的独立模块负责，不作为本链路的章节或单元任务；也不要将这些职责换名为“背景”“概述”等章节继续规划。理解后续分析所必需的背景、概念、理论教学、机制、方法比较，以及具有具体问题与分析的未来研究方向，仍属于正文，应按解释需要充分展开。每章应有明确的知识问题或分析任务，不能仅以引入全文或总结全文为目的。本链路仍须确定研究范围、组织视角和章节分工，但这些规划信息不等于需要写成一个入口章节。上述职责归属适用于本链路所有阶段。\n\n"
        "章节组织和比较尺度由研究问题、学科语境与现有材料决定，不预设任何单一研究范式为所有主题的共同模板；理论、概念、方法论和设计研究也应按其实际论证结构组织。"
        "以用户研究问题为中心，提出贯穿全文的组织视角和综合判断，再让不同来源承担解释、比较、例证、背景或发展等职责；不要按论文逐篇复述，也不要让某个局部缺口取代全篇主线。综合判断必须由材料中的联系、差异或演进支撑。"
        "根据研究问题和材料本身判断相关性；材料中的推荐语可能夸大用途，应以来源实际的研究对象或材料、研究设置、方法、比较、结果或论证、验证方式和边界为准。"
        "保留每个来源真实的研究类型、对象、设置、条件、方法、结果和限制。综述的综合、假设或方案不是已观察到的结果。"
        "相关时，综述中报告的原始研究与直接来源享有同等实质使用权。保留报告综述的来源元数据；原始研究身份已解析时，写作者可以直接引用原始研究，不因材料来自专家综述就要求逐句降格或反复标注‘综述转述’，也不要声称读过原始全文或重新获取它。"
        "保留每个报告综述自己的 R# 命名空间；有 source_handle 就使用它，否则原样保留 supplied paper_id 或 reference。短 handle 到规范身份的映射由本地程序完成。证据足够时使用广泛材料，但不要为了凑数量硬塞来源。"
        "区分关联与因果证据、机制或解释、迁移或应用、验证和不确定性；不要把相关关系、作者提出的解释或同一组材料内的支持误写成因果机制、跨设置迁移或独立验证。"
        "有限补充检索没有找到匹配研究，不等于该研究不存在，也不等于整个领域缺乏相关研究或不存在更优路线；未被满足的需求只约束本次可写的结论范围，不得升级为领域层面的缺失判断，也不应让同一缺口反复主导多个章节。只描述现有材料支持的范围，并纠正前面规划中的过度表述。路由摘要与原始 interpretation_limits 冲突时，以原始限制为准。综述题名中的对象或应用也不自动成为其中每项研究的对象和设置。"
        "研究条件必须进入 substantive_point、thesis、synthesis 和 transition 本身；决定含义的研究对象与研究设置随主张、案例和展开关系一起陈述，而不是全部堆进独立的 limitation 字段；不能靠单独的 limitation 字段修补前文过度断言，也不要求每句都填条件表或给所有判断统一加“可能”。对不同对象、设置或比较尺度的材料，保留其不可直接等同之处，不把它们排成脱离条件的效果排名。"
        "章节标题也用中文。Return JSON only."
    )
    stage_specific = {
        "provisional_scope": (
            "Read every item in candidate_pool. It is the complete B pool, not a sample. Screen all items semantically and "
            "build a source-informed theme inventory from the evidence summaries before drafting the provisional scope. "
            "材料充分时追求充分覆盖：背景、具体案例、比较与研究发展材料都得到安排（材料池规模见 pool_row_count）；这是覆盖质量目标，不是计数门槛，绝不为数量塞入无关论文，也不因池小而放弃覆盖。 "
            "Propose a provisional shared scope, central thesis, chapter outline, cross-chapter boundaries, and concise tool "
            "requests. Do not rely on a small prefix or require a per-paper rejection explanation; grouped non-use reasons are fine. "
            "Return keys: review_title, central_question, provisional_scope, material_theme_inventory, provisional_outline "
            "(chapters with chapter_id/title/purpose/question), cross_chapter_rules, grouped_screening_notes, supplement_requests, "
            "directed_reads. Every source request must name source_handle; gaps use gap_id. supplement_requests entries need gap_id, gap_question, "
            "success_criteria, chapter_ids, intended_use (background/mechanism/comparison/quantification/application_outcome/study_design), targeted_queries, optional reuse_plan_facet_ids, known_papers, reviewed_references. targeted_queries are 1-2 compact English keyword queries (object + specific relation) or self-contained English semantic statements, never generic questions without the domain object. "
            "directed_reads entries need paper_id, chapter_ids, questions, required_outputs, knowledge_gap, and a reason."
        ),
        "level1_outline": (
            "Finalize the first shared outline using the provisional scope and the actual level-1 supplement and directed "
            "reading results. Explain how successful, partial, failed, or unavailable tool outputs changed chapter scope. "
            "Adapt where evidence is missing; do not request the same missing item repeatedly. Return shared_scope, "
            "shared_outline (ordered chapters), review_argument, source_selection_principles, and unresolved_limits."
        ),
        "source_routing": (
            "本次 candidate_batch 是完整 B 池按顺序分批后的一个批次，不是抽样。逐篇阅读本批次每一篇的 B 规划卡，"
            "将每篇都路由到 L1 共享提纲中适合的章节。判断时检查论文实际研究类型、对象、条件、材料深度与范围解释提醒；"
            "不要只依据宽泛或夸大的用途推荐语。A 卡留给后续章节细化。一个来源可进入多个章节；确实不相关时 chapter_ids 为空并简短说明。"
            "每篇必须返回 source_handle、paper_title、chapter_ids、specific_usable_material。specific_usable_material 用中文写约"
            "80–150 字，指出具体可用事实或比较角度及其在本综述中的用途；不相关来源只需简短解释。不得只挑少数种子文献，"
            "也不得为了覆盖率强行安排无关材料。返回包含 source_routes 数组的 JSON 对象，每个输入 source_handle 恰好出现一次，不增造编号。"
        ),
        "chapter_proposals": (
            "当前调用只负责 chapter 指定的一章；shared_level1_outline 用来理解全篇分工。只返回该章的提案。"
            "source_routing 已由程序完整保留，不要重复抄写全量 source_handles；只在具体案例、比较或排除项中引用必要编号。"
            "把输出集中于论述结构、研究差异、衔接和有价值的缺口。"
            "Use the complete source_routing ledger, which covers every source in the full B pool. Do not discard routed sources "
            "merely because they were not deep-read; the route ledger contains the source-specific A/B facts needed for broad planning. "
            "The L1 outline is an initial direction, not a binding template: correct its thesis, scope, chapter titles or emphasis where "
            "the full routed evidence shows an overclaim, missing context, or better organizing logic, while retaining chapter_id values "
            "where possible so tool requests remain attached. Preserve the broad set of useful source handles assigned by routing; do not "
            "collapse chapters to a few deep-read seed papers. When material supports it, plan a full-length review with broad coverage "
            "across background, concrete cases, comparisons, and developments (the pool size is in candidate_pool_row_count); this is a "
            "coverage aspiration, never a count gate or reason to include irrelevant sources, nor a reason to shrink to a few core papers. "
            "Draft a substantive proposal for each "
            "chapter, including relevant source handles, chapter boundaries, ordered themes, concrete cases/comparisons, "
            "cross-paper synthesis, conditions and limitations, transition logic, and any genuinely necessary tool needs. "
            "Do not silently drop a routed source: when an individual source is genuinely out of scope, name it in "
            "excluded_source_handles with a concise reason; otherwise retain it as writer-available material. "
            "Return chapter_proposals (each with chapter_id, title, purpose, scope, optional excluded_source_handles, substantive_threads, "
            "supplement_requests, directed_reads) and screened_sources with non-use reasons. A source may support multiple "
            "chapters when its evidence actually does so."
        ),
        "harmonize_scope": (
            "论文的完整章节分配由本地程序继承，不要在输出中重抄各章 source_handles/source_ids 清单；"
            "只输出必要的具体研究引用、明确排除项和结构调整。"
            "Act as the central review editor. Reconcile chapter proposals before detailed expansion. Set one shared scope, "
            "ordered outline, chapter boundaries, concepts, terminology, and transitions. Remove overlap and resolve "
            "contradictions between proposals while preserving their broad useful source assignments; do not replace routed literature "
            "with only deep-read seeds. Remove a routed source only when it is explicitly named in excluded_source_handles with a reason; "
            "otherwise inherit it into the coordinated chapter source list. Merge and deduplicate supplement gaps and directed-read tasks by source_handle "
            "while preserving all distinct questions and chapter_ids. Preserve valuable level-1 tool results and do not "
            "reacquire review-reported original full texts. Return shared_outline, chapters (with chapter_id and coordinated "
            "scope), supplement_requests, directed_reads, and harmonization_notes."
        ),
        "finalize_chapter_scope": (
            "本地程序保留此前的论文分配，输出不必重复完整编号清单；集中改好结构与论述任务。"
            "这一阶段只确定每章的范围与主要论述线索，每章约300–600字；具体段落、案例组与大量引用留给下一步章节细化。"
            "Finalize the coordinated chapter scopes after reviewing actual level-2 supplement and directed-reading returns, "
            "including failures, partial results, newly added sources, and upgraded planning material. Update source assignments "
            "and unit emphasis where evidence warrants. Keep the already harmonized boundaries and shared transitions coherent. "
            "Retain routed assignments unless explicitly naming a source_handle in excluded_source_handles with a concise reason. "
            "Do not issue new tool requests or repeat missing-material demands. Return shared_outline, chapters (chapter_id, "
            "title, purpose, scope, substantive_threads), and final_scope_notes."
        ),
        "chapter_details": (
            "Prepare this chapter's writer-ready plan from the coordinated shared scope and supplied A/B/deep materials. "
            "The chapter object is your only writing assignment. The shared_outline is a map of OTHER chapters as well, "
            "not a list to reproduce as this chapter's units. Keep this chapter within its purpose and scope; briefly signpost "
            "topics owned by other chapters instead of developing them again. Treat the outline as an editorial "
            "assignment, not a factual source: correct factual or terminology mistakes using the supplied A/B and reading material. "
            "Develop the chapter across the supplied collection, not only a few deeply read examples. Organize useful studies "
            "into concrete case and comparison groups attached to the actual content units; identify what each group contributes. "
            "The writer should receive enough selected cases, contrasts and background sources for a substantial review chapter. "
            "Do not do new research. Return a chapter_plan with a clear thesis, reader objective, ordered substantive units, "
            "specific cases and paper references, cross-paper synthesis, conditions and limitations, and transitions. Every "
            "unit must state (1) its substantive point, (2) the ordered development, (3) concrete studies or cases with "
            "source_handle only (the local program supplies bibliographic identity) together with the research object and "
            "setting that determine what the finding means, (4) what findings agree or conflict and why, (5) evidence "
            "conditions and limits kept with the claims they qualify rather than parked in a separate list, and (6) its "
            "transition. Within each substantial unit, provide paragraph_briefs: an ordered list of "
            "paragraph tasks, each with its specific point, development (the actual comparison or reasoning), and source_handles. "
            "A unit may span several distinct paragraphs; do not reduce a rich theme to 'introduce, discuss, summarize'. "
            "Let useful distinctions in the material determine the number of paragraphs, not a fixed quota. "
            "Keep direct-source references distinct from review-reported references."
        ),
        "chapter_need_analysis": (
            "Inspect each finalized chapter scope for concrete unresolved evidence needs that would materially improve its writer plan. "
            "Use the supplied source-specific material, not source identifiers alone, to decide what is actually missing. Return only high-value, chapter-owned supplement_requests and directed_reads; keep each question narrow and attach chapter_ids. Each supplement needs gap_id, gap_question, success_criteria, intended_use (background/mechanism/comparison/quantification/application_outcome/study_design) and 1-2 targeted_queries (query_type keyword or question, query_text in English). State the domain object and specific relation in every query. Prioritize the few gaps that change the argument or supply a missing comparison; avoid demands for every desirable detail. "
            "Do not repeat a settled level-1 or level-2 request, and return empty arrays when the supplied material is sufficient."
        ),
        "whole_plan_improvement": (
            "Make one light whole-plan improvement pass. Calibrate the supplied review_argument against the actual "
            "material and chapter plans, keeping it distinct from shared_scope (coverage and exclusions). Return a "
            "complete finalized_review_argument, preserving the argument when supported and qualifying or correcting "
            "it when necessary. Do not substitute a scope statement for an argument. Identify only high-value changes to flow, duplication, scope "
            "consistency, terminology, and transitions. Coordinate by actual content: for each important concept, "
            "mechanism or method, say which chapter is its primary place of explanation and what a re-appearance in "
            "another chapter adds for the reader. Judge duplication by explanatory role, not by sentence similarity or "
            "repeated citations: the same paper may legitimately support several chapters with different uses, so do not "
            "remove or reassign content merely because a source or a phrasing recurs. A locally unmet retrieval need only "
            "bounds what the affected chapter can currently conclude; it must not become a field-level absence claim in "
            "any chapter. Do not replace chapter evidence, invent citations, demand new "
            "research, or reopen settled tool gaps. Return concise improvement_notes, cross_chapter_adjustments, "
            "updated_chapter_plans only for small scalar fields such as thesis, title, or reader objective, and small "
            "shared_outline_adjustments. Do not return full chapter plans or units: if unit structure needs revision, "
            "return a concise chapter-level feedback item naming the chapter and the reason, so a separate full chapter "
            "revision can use the complete evidence. Identify every edit by chapter_id and describe the actual replacement "
            "or development needed; the chapter reviser will implement it. When shared guidance changes, optionally return complete finalized_shared_scope and "
            "finalized_harmonization_notes replacements. Do not return or invent citation policy; the program's current "
            "citation_rules remain authoritative."
        ),
        "affected_chapter_revision": (
            "Turn the supplied whole-plan improvement notes into concrete writer-plan edits for the named chapters. "
            "Use the supplied source_materials (the chapter's cited A/B evidence) and related tool_materials to correct "
            "factual, study-type, citation, condition, or limitation mistakes. Treat chapter_feedback as an editorial "
            "request, not as evidence; do not repeat an unsupported claim from it. Return a complete replacement plan "
            "including every existing substantive unit, even when only one unit needs correction. "
            "For broad units that still leave the writer to invent the argument, add ordered paragraph_briefs with point, "
            "development and source_handles. Use the supplied studies to give a concrete comparison, mechanism or example "
            "for each paragraph task, rather than just listing topics. Existing material can support a richer explanation "
            "without new research. Do not let one unanswered retrieval need dominate the review's whole intellectual framing. "
            "Preserve each chapter's evidence, source handles, conditions and limitations, and change only the requested "
            "flow, duplication, scope, terminology, or transition issue. Return chapter_updates, with one item per target "
            "containing chapter_id and a complete updated_plan. Do not add sources, invent findings, or return notes without "
            "an updated_plan."
        ),
        "case_groups": (
            "为已经协调好的写作单元挑选可用文献案例。source_materials 提供本批每篇候选来源的实际材料"
            "（A 概括、B 综述规划，以及已有的精读、补充或本地片段）；阅读全部 source_routing 与 source_materials，"
            "再对照 unit_catalog，挑选材料确实支持本单元的对象、结果、条件或有用对照。"
            "整篇长综述以约150–200篇具有明确用途的独立文献为目标（不是每章或每批的目标，已有案例计入覆盖）；"
            "这是质量目标不是数量门槛：不重复添加已有来源，不为数量塞入无关文献，也不把“只用少数核心论文”当统一规则。"
            "只为具体单元补材料，不改章节结构。"
            "对照已有 cases/supporting_studies 的具体贡献，新论文需补充不同结果、条件、方法、发展阶段或有用对照；仅重复相同概括时留在备选池，additions可为空。"
            "每篇写约30–60字说明这篇材料能帮助本单元解释什么，可以提出拟议综合；"
            "这段文字是具体写作用途，不是论文结果；论文的方法、结果与结论必须来自随附的 A/B、精读或补充材料。"
            "推荐用途不能把类比、方案或其他对象的结果说成本问题的直接实证；material_available 为假的来源没有实际内容，不得凭编号编造用途。"
            "当前调用只负责一个 chapter_id；完整 source_routing 可能已按章节截取，不能据此虚构遗漏来源。"
            "返回 JSON 对象：additions 数组，每项 unit_key、studies 数组（source_handle、contribution）。"
        ),
    }
    stage_text = stage_specific[stage]
    if planning_revision and stage == "case_groups":
        stage_text = stage_text.replace(
            "每篇写约30–60字说明这篇材料能帮助本单元解释什么，可以提出拟议综合；",
            "每篇写 contribution：约30–60字说明这篇材料能帮助本单元解释什么（具体写作用途），可以提出拟议综合；",
        )
    if planning_revision:
        stage_text = stage_text.replace("跨研究比较和衔接", "有材料依据的论证关系和衔接")
        stage_text = stage_text.replace("what findings agree or conflict and why", "material-supported relations, conditions or limits")
        stage_text = stage_text.replace("finding or argument, comparison, mechanism or example", "finding or argument, mechanism, concept, development or example")
        stage_text = stage_text.replace("a concrete comparison, mechanism or example", "a concrete argument, mechanism, concept or example")
        stage_text = stage_text.replace(
            "Preserve each chapter's evidence, source handles, conditions and limitations, and change only the requested "
            "flow, duplication, scope, terminology, or transition issue.",
            "Preserve each chapter's evidence, source handles, conditions and limitations; where the supplied material shows "
            "that the thesis, scope, terminology, judgment, or unit structure needs correction, implement that owner-level update "
            "instead of treating a short scalar replacement as authoritative.",
        )
        role_directives = {
            "chapter_details": (
                "章节负责人在掌握 A/B/精读材料后建立有材料依据的科学认识、thesis、reader_objective 和具体展开关系。"
                "决定含义的研究对象与研究设置随主张、案例和展开关系一起陈述；limitations 只收尚未随判断说明的剩余边界，"
                "不把条件全堆进去，不逐句填条件表，也不给所有判断统一加“可能”。"
                "工具未回答的需求只约束当前可写结论，不得写成领域缺失或不存在更优路线，也不让同一缺口主导本章结构。"
                "不要求每个单元都比较研究结果，也不要求每次比较都解释 why；材料若只支持概念关系、方法前提、机制、发展、背景、"
                "例证或边界，就按该实际功能组织。只有材料确有可比对象和依据时才比较，并明确比较尺度；没有依据时不得补造差异原因。"
                "相互独立的维度可以并存；除非材料明确支持，不把它们写成互斥且穷尽的二分路径。"
            ),
            "affected_chapter_revision": (
                "章节负责人可以依据 supplied source_materials 和 late material 修正原有 thesis、reader_objective、判断和单位结构；"
                "这些更新必须同时保留原判断与新增材料的边界，不把编辑意见或单一标签当作证据。按材料实际功能组织，不强制比较或解释差异原因。"
                "决定含义的对象与设置随修正后的主张一起保留；未被满足的需求仍只约束可写结论，不升级为领域判断。"
                "相互独立的维度可以并存；除非材料明确支持，不把它们写成互斥且穷尽的二分路径。"
                "案例选择由后续 case_groups 依据随附的实际材料直接补入正文计划；本阶段不接收或采纳 case_suggestions。"
            ),
            "whole_plan_improvement": (
                "全局协调只检查范围、章节分工、衔接和材料影响；对重要概念、机制或方法写明主讲章与再现章各自增加的解释，"
                "按解释职责判断重复——允许同一论文跨章按不同用途复用，不按句子相似或引用重复删内容。"
                "发现实质 thesis/判断变化时输出具体 chapter feedback 交章节负责人落实，协调结论要落到必要章节的更新，不是只列一张建议表；"
                "不要用短 scalar 或 chapter_argument 直接替代章节科学认识。跨章调整保持有界。"
            ),
            "case_groups": (
                "案例扩展只做选材：阅读所附 source_materials 的实际材料，返回来源指针、单元归属和具体写作用途（contribution）；"
                "contribution 只说明该来源在本单元承担的解释、比较、背景或例证职责，不是论文结果。具体科学内容由随附的 A/B、精读或补充材料提供，"
                "本阶段直接把已选案例追加到正文计划，不再等待后续负责人确认。"
            ),
        }
        directive = role_directives.get(stage, (
            "本模式只在显式 opt-in 时启用。章节负责人建立有材料依据的认识；其他角色只能执行其职责并回传具体问题。"
        ))
        stage_text += "\n\n【章节论证模式】" + directive
    return common + "\n\n" + stage_text


def _compact_reading_material(raw: Any) -> Any:
    """Keep extracted content once and only the bibliography it actually uses."""
    if not isinstance(raw, Mapping):
        return raw
    result = dict(raw)
    questions = result.get("question_material")
    if questions:
        result.pop("content", None)
        result.pop("open_questions", None)
    used: set[str] = set()
    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key == "reference_ids" and isinstance(item, list):
                    used.update(str(ref) for ref in item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(questions or result.get("content") or {})
    if isinstance(result.get("references"), list):
        result["references"] = [ref for ref in result["references"] if isinstance(ref, Mapping) and str(ref.get("reference_id")) in used]
    return result


def _candidate_card_material(candidate: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return the saved A, B and identity-facing material for one candidate.

    Candidate navigation is allowed to expose a source that was not selected by
    the current chapter.  Reading the existing card here keeps that operation
    local and preserves the same A/B distinction used by the regular chapter
    payload.
    """

    card = _card_for_candidate(candidate)
    a = card.get("general_understanding") if isinstance(card.get("general_understanding"), Mapping) else {}
    b = card.get("review_planning") if isinstance(card.get("review_planning"), Mapping) else {}
    planning = candidate.get("planning_view") if isinstance(candidate.get("planning_view"), Mapping) else {}
    identity = planning.get("paper_identity") if isinstance(planning.get("paper_identity"), Mapping) else {}
    source_identity = {**dict(identity), **{key: candidate[key] for key in ("paper_id", "canonical_paper_id", "doi") if candidate.get(key)}}
    if candidate.get("_paper_id"):
        source_identity["paper_id"] = candidate["_paper_id"]
    conflict = _saved_card_identity_conflict(source_identity, card)
    if conflict is not None:
        a, b = {}, {}
    return dict(a), dict(b), {
        **({"material_identity_conflict": True,
            "material_identity_conflicts": [{"channel": "saved_card", "reason": "source_identity_conflict",
                "card_identity": {k: conflict.get(k) for k in ("paper_id", "canonical_paper_id", "doi", "title") if conflict.get(k)}}]}
           if conflict is not None else {}),
        "title": _text(identity.get("title") or candidate.get("title")),
        "doi": _text(identity.get("doi") or candidate.get("doi")),
        "year": _text(identity.get("year") or candidate.get("year")),
        "material_depth": _text(
            (candidate.get("_b_summary") or {}).get("declared_content_depth")
            if isinstance(candidate.get("_b_summary"), Mapping)
            else planning.get("declared_content_depth")
        ),
    }


def build_local_material_payload(
    candidate: Mapping[str, Any],
    *,
    index: Any | None = None,
    index_path: str | Path | None = None,
    question: str = "",
    hits: Sequence[Any] = (),
    deep_material: Mapping[str, Any] | None = None,
    passages_per_paper: int = 2,
    passage_chars: int = 1200,
) -> dict[str, Any]:
    """Build a bounded, local-only material record for a candidate source.

    The record deliberately contains the saved A/B card and any focused local
    passages supplied by ``PlanningMaterialIndex``.  A caller may also pass a
    previously saved directed-reading result as ``deep_material``.  This is an
    input-construction helper: it never contacts a provider and never upgrades
    an abstract/card into full text.
    """

    a, b, identity = _candidate_card_material(candidate)
    paper_id = _text(candidate.get("_paper_id") or _canonical_paper_id(candidate))
    source_handle = _text(candidate.get("_source_handle"))
    material: dict[str, Any] = {
        "source_handle": source_handle,
        "paper_id": paper_id,
        "title": identity["title"],
        "doi": identity["doi"],
        "year": identity["year"],
        "material_depth": identity["material_depth"],
        "study_summary_A": a,
        "review_planning_B": b,
        "external_calls": 0,
        **{key: identity[key] for key in ("material_identity_conflict", "material_identity_conflicts") if key in identity},
    }
    # Supplement results are persisted on pool rows under the gap-specific
    # names. Keep them in candidate fallback payloads so a selected source
    # does not lose the only substantive material it has.
    if candidate.get("supplement_gap_material"):
        material["supplement_gap_material"] = candidate.get("supplement_gap_material")
        material["supplement_material"] = candidate.get("supplement_gap_material")
    if candidate.get("supplement_gap_materials"):
        material["supplement_gap_materials"] = candidate.get("supplement_gap_materials")
        material["supplement_materials"] = candidate.get("supplement_gap_materials")
    if deep_material and _saved_card_identity_conflict(_candidate_identity(candidate), deep_material) is None:
        material["deep_read_material"] = _compact_reading_material(deep_material)

    bounded_hits: list[dict[str, Any]] = []
    for raw in list(hits)[: max(0, int(passages_per_paper))]:
        if hasattr(raw, "to_dict") and callable(raw.to_dict):
            row = dict(raw.to_dict())
        elif isinstance(raw, Mapping):
            row = dict(raw)
        else:
            continue
        if _text(row.get("paper_id")) and _text(row.get("paper_id")) != paper_id:
            continue
        text = _text(row.get("text"))
        if passage_chars > 0 and len(text) > passage_chars:
            text = text[:passage_chars].rstrip() + "…"
        row["text"] = text
        if isinstance(row.get("best_sentence"), str) and passage_chars > 0:
            row["best_sentence"] = row["best_sentence"][:passage_chars]
        bounded_hits.append(row)
    if bounded_hits:
        material["local_passages"] = {
            "reading_mode": "focused_local_search",
            "question": question,
            "passages": bounded_hits,
            "external_calls": 0,
        }
    if deep_material and isinstance(material.get("deep_read_material"), Mapping):
        material["deep_read_material"] = {
            **dict(material["deep_read_material"]),
            "reading_mode": "focused_local_search",
        }
    if material.get("material_identity_conflict") and _owner_material_has_content({**material, "material_identity_conflict": False}):
        # The card channel was removed above; its diagnostic cannot disqualify
        # independently supplied, compatible reading/supplement content.
        material.pop("material_identity_conflict", None)
    return material


def build_candidate_navigation(
    *,
    chapter: Mapping[str, Any],
    candidates: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]],
    source_routes: Sequence[Mapping[str, Any]] = (),
    research_question: str = "",
    index_path: str | Path | None = None,
    candidate_limit: int = 12,
    passages_per_paper: int = 2,
    passage_chars: int = 1200,
    deep_material_by_paper: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Expose bounded candidate navigation and local material access.

    The chapter model receives identities and reasons for the candidates that
    are relevant to this chapter, including relevant sources outside its old
    ``source_ids``.  Only top local hits receive passage content; the complete
    pool is retained by the program and remains reachable through the recorded
    local index and candidate handles.
    """

    if isinstance(candidates, Mapping):
        rows = [row for row in candidates.values() if isinstance(row, Mapping)]
    else:
        rows = [row for row in candidates if isinstance(row, Mapping)]
    by_id = {_text(row.get("_paper_id") or _canonical_paper_id(row)): row for row in rows if _text(row.get("_paper_id") or _canonical_paper_id(row))}
    by_handle = {_text(row.get("_source_handle")): row for row in rows if _text(row.get("_source_handle"))}
    chapter_id = _text(chapter.get("chapter_id") or chapter.get("id"))
    source_ids: set[str] = set()
    for key in ("source_ids", "paper_ids", "source_handles"):
        values = chapter.get(key) or []
        if isinstance(values, str):
            values = [values]
        if isinstance(values, Sequence):
            source_ids.update(_text(value) for value in values if _text(value))
    assigned_ids = {
        _text(row.get("_paper_id") or _canonical_paper_id(row))
        for row in rows
        if _text(row.get("_paper_id") or _canonical_paper_id(row)) in source_ids
        or _text(row.get("_source_handle")) in source_ids
    }
    route_by_handle: dict[str, dict[str, Any]] = {}
    for raw in source_routes:
        if not isinstance(raw, Mapping):
            continue
        handle = _text(raw.get("source_handle") or raw.get("source_id"))
        if handle and handle not in route_by_handle:
            route_by_handle[handle] = dict(raw)
    route_ids: set[str] = set()
    for handle, route in route_by_handle.items():
        chapter_ids = route.get("chapter_ids") or []
        if isinstance(chapter_ids, str):
            chapter_ids = [chapter_ids]
        if chapter_id and chapter_id in {_text(item) for item in chapter_ids} and handle in by_handle:
            route_ids.add(_text(by_handle[handle].get("_paper_id") or _canonical_paper_id(by_handle[handle])))

    parts = [research_question, _text(chapter.get("title")), _text(chapter.get("purpose")), _text(chapter.get("scope"))]
    for key in ("substantive_threads", "themes", "focus", "material_questions"):
        value = chapter.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            parts.extend(_text(item.get("point") if isinstance(item, Mapping) else item) for item in value)
        elif value:
            parts.append(_text(value))
    question = " ".join(item for item in parts if item).strip()[:1200]

    hits_by_paper: dict[str, list[Any]] = {}
    card_matches: dict[str, dict[str, Any]] = {}
    search_meta: dict[str, Any] = {"status": "not_requested", "external_calls": 0}
    if index_path:
        try:
            from .planning_material_search import PlanningMaterialIndex, paper_context, search

            with PlanningMaterialIndex(Path(index_path), readonly=True) as index:
                result = search(
                    index,
                    question or _text(chapter_id),
                    top_papers=max(1, int(candidate_limit) * 2),
                    passages_per_paper=max(1, int(passages_per_paper)),
                )
                for hit in result.hits:
                    hits_by_paper.setdefault(_text(hit.paper_id), []).append(hit)
                contexts = paper_context(index, list(hits_by_paper))
                search_meta = {
                    "status": "matched" if result.found else "empty",
                    "question": result.question,
                    "candidate_papers": result.candidate_papers,
                    "matched_papers": result.matched_papers,
                    "index_papers": result.index_papers,
                    "index_segments": result.index_segments,
                    "not_matched_reason": result.not_matched_reason,
                    "paper_contexts": [item.to_dict() for item in contexts],
                    "external_calls": 0,
                }
        except Exception as exc:
            # A missing or stale local index should be visible to the caller,
            # but it must not turn the optional path into an external search.
            search_meta = {"status": "unavailable", "error": type(exc).__name__, "external_calls": 0}

    # A planner run may have saved A/B cards before a SQLite index exists.
    # Reuse those cards as a bounded lexical navigation pass so an unassigned
    # tail source remains discoverable without a network call.  Card matches are
    # never represented as deep/full-text passages.
    if not hits_by_paper:
        try:
            from .planning_material_search import tokenize

            query_tokens = set(tokenize(question))
            for paper_id, candidate in by_id.items():
                a, b, _identity = _candidate_card_material(candidate)
                card_tokens = set(tokenize(json.dumps({"A": a, "B": b}, ensure_ascii=False, default=_json_default)))
                matched = sorted(query_tokens & card_tokens)
                if matched:
                    card_matches[paper_id] = {
                        "score": len(matched) / max(1, len(query_tokens)),
                        "match_terms": matched,
                    }
            if card_matches:
                search_meta = {
                    "status": "card_fallback",
                    "question": question,
                    "candidate_papers": len(card_matches),
                    "matched_papers": len(card_matches),
                    "index_papers": 0,
                    "index_segments": 0,
                    "external_calls": 0,
                }
        except Exception as exc:
            search_meta = {"status": "card_fallback_unavailable", "error": type(exc).__name__, "external_calls": 0}

    hit_order = sorted(
        hits_by_paper,
        key=lambda paper_id: max(
            float(getattr(hit, "score", 0.0) if not isinstance(hit, Mapping) else hit.get("score") or 0.0)
            for hit in hits_by_paper[paper_id]
        ),
        reverse=True,
    )
    card_order = sorted(card_matches, key=lambda paper_id: (-float(card_matches[paper_id]["score"]), paper_id))
    selected_ids: list[str] = []
    for paper_id in [*sorted(assigned_ids), *sorted(route_ids), *hit_order, *card_order]:
        if paper_id and paper_id in by_id and paper_id not in selected_ids:
            selected_ids.append(paper_id)
    limit = max(1, int(candidate_limit))
    # Always retain local-search hits, even if the assigned list is long.  This
    # is what makes a relevant tail candidate reachable without stuffing the
    # entire pool into the prompt.
    must_keep = [paper_id for paper_id in [*hit_order, *card_order] if paper_id in selected_ids]
    keep_ids = list(dict.fromkeys([*must_keep, *selected_ids]))[:limit]

    navigation: list[dict[str, Any]] = []
    candidate_materials: list[dict[str, Any]] = []
    for paper_id in keep_ids:
        candidate = by_id.get(paper_id)
        if not candidate:
            continue
        handle = _text(candidate.get("_source_handle"))
        route = route_by_handle.get(handle, {})
        route_chapters = route.get("chapter_ids") or []
        if isinstance(route_chapters, str):
            route_chapters = [route_chapters]
        a, b, identity = _candidate_card_material(candidate)
        hit_rows = hits_by_paper.get(paper_id, [])
        card_match = card_matches.get(paper_id) or {}
        navigation.append({
            "source_handle": handle,
            "paper_id": paper_id,
            "title": identity["title"],
            "doi": identity["doi"],
            "year": identity["year"],
            "material_depth": identity["material_depth"],
            "assignment": {
                "selected_in_chapter": paper_id in assigned_ids,
                "routed_to_chapter": paper_id in route_ids,
                "routed_chapter_ids": [_text(item) for item in route_chapters if _text(item)],
                "route_status": _text(route.get("route_status")),
            },
            "material_available": {
                "A": bool(a),
                "B": bool(b),
                "deep": bool((deep_material_by_paper or {}).get(paper_id)),
                "passages": bool(hit_rows),
                "local_index": bool(index_path),
            },
            "retrieval": {
                "source": "local_search" if (hit_rows or card_match) else ("chapter_assignment" if paper_id in assigned_ids else "source_routing"),
                "match_terms": sorted({
                    *[term for hit in hit_rows for term in (getattr(hit, "match_terms", ()) if not isinstance(hit, Mapping) else hit.get("match_terms") or [])],
                    *[str(term) for term in card_match.get("match_terms") or []],
                }),
                "score": max(
                    [float(getattr(hit, "score", 0.0) if not isinstance(hit, Mapping) else hit.get("score") or 0.0) for hit in hit_rows]
                    + [float(card_match.get("score") or 0.0), 0.0]
                ),
            },
        })
        if (hit_rows or card_match) and paper_id not in assigned_ids:
            candidate_materials.append(build_local_material_payload(
                candidate,
                hits=hit_rows,
                question=question,
                deep_material=(deep_material_by_paper or {}).get(paper_id),
                passages_per_paper=passages_per_paper,
                passage_chars=passage_chars,
            ))

    unassigned = [row for row in navigation if not row["assignment"]["selected_in_chapter"]]
    return {
        "chapter_id": chapter_id,
        "query": question,
        "candidate_count": len(rows),
        "returned_count": len(navigation),
        "unassigned_relevant_count": len(unassigned),
        "candidates": navigation,
        "candidate_materials": candidate_materials,
        "source_handles": [row["source_handle"] for row in navigation if row.get("source_handle")],
        "omitted_candidate_count": max(0, len(rows) - len(navigation)),
        "local_search": search_meta,
        "access_contract": {
            "material_fields": ["study_summary_A", "review_planning_B", "local_passages", "deep_read_material"],
            "external_calls": 0,
            "unassigned_material_is_optional": True,
        },
    }


def _messages_for(stage: str, payload: Mapping[str, Any]) -> list[dict[str, str]]:
    rendered_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_json_default)
    planning_revision = bool(payload.get("planning_revision_mode"))
    # BODY case additions use one formal contract in both modes.  The
    # contribution is a writing purpose; attached source material remains the
    # authority for scientific facts.
    case_field = "contribution"
    task_output = {
        "chapter_need_analysis": (
            '本轮只做取材任务规划，不生成或复述综述大纲。只返回 {"supplement_requests": [...], "directed_reads": [...]}。'
            "每项必须说明它具体解决哪个章节问题；已有材料能写好则返回空数组。不要返回 chapters、source_ids、论文清单或已有综述正文。"
        ),
        "case_groups": f'本轮只返回 {{"additions": [{{"unit_key": "...", "studies": [{{"source_handle": "...", "{case_field}": "..."}}]}}]}}，不复述输入。',
        "source_routing": '本轮只返回 {"source_routes": [...]}，逐篇给出 source_handle、chapter_ids、specific_usable_material，不重写大纲。',
        "chapter_details": '本轮只返回 {"chapter_plan": {...}}：thesis、reader_objective、units。每个实质单元包含 paragraph_briefs（逐段的point、development、source_handles）、案例对象（source_handle、finding、conditions，含决定含义的研究对象与设置；条件随所属判断陈述，不整体堆入 limitations）、跨研究比较和衔接。不能只列论文编号，也不能用一个宽泛展开句代替逐段任务。不要返回输入的 source_materials 或论文库存清单。',
        "affected_chapter_revision": '只返回 {"chapter_updates": [{"chapter_id": "...", "updated_plan": {...}}]}。updated_plan是完整章节，保留有效论述与案例，并落实反馈。各实质单元给出paragraph_briefs，每段写清point、development、source_handles，让写作者无需重新发明论证。',
    }.get(stage, "请用中文撰写本阶段要求的内容，只返回本阶段的JSON结果，不照抄输入字段。")
    if planning_revision and stage == "chapter_details":
        task_output = '本轮只返回 {"chapter_plan": {...}}：thesis、reader_objective、units。每个实质单元包含 paragraph_briefs（逐段的point、development、source_handles）、案例对象（source_handle、finding、conditions，含决定含义的研究对象与设置；条件随所属判断陈述，不整体堆入 limitations）、论证关系和衔接；按材料实际功能组织，不强制比较或解释差异原因。不能只列论文编号，也不能用一个宽泛展开句代替逐段任务。不要返回输入的 source_materials 或论文库存清单。'
    if planning_revision and stage == "affected_chapter_revision":
        task_output = (
            '本轮只返回 {"status":"updated"或"no_change", "chapter_updates":[{"chapter_id":"...", "updated_plan":{...}}], '
            '"unit_id_remap":{"new_unit_id":["old_unit_id"]}}。updated_plan 是完整章节计划；'
            '若材料和任务无需改变，明确返回 status=no_change 并可复用完整原计划。若拆分或合并单元，所有新单元必须有稳定 unit_id，'
            '并提供显式 unit_id_remap，不能按列表位置猜测对应关系。不要返回只有说明没有 updated_plan 的成功结果。'
        )
    return [
        {"role": "system", "content": _planner_instructions(stage, planning_revision=planning_revision)},
        {"role": "user", "content": "当前任务：" + stage + "\n" + rendered_payload + "\n\n【本轮交付】" + task_output},
    ]


def _chapter_details_capacity(
    planner: Any,
    payload: Mapping[str, Any],
    *,
    max_input: int = MAX_INPUT_TOKENS,
    max_total: int = 1_000_000,
) -> dict[str, Any] | None:
    """Estimate a complete chapter-details request using the live planner settings.

    The ordinary injected test planners do not need a tokenizer.  They keep
    the legacy call path, while the real ``QwenProgressivePlanner`` exposes
    the same counter/output settings used by its provider preflight.
    """

    counter = getattr(planner, "counter", None)
    if not callable(counter):
        return None
    output_tokens = min(int(getattr(planner, "output_tokens", 16_000)), 16_000)
    thinking_tokens = 2_048  # QwenProgressivePlanner.__call__ uses this for chapter_details.
    messages = _messages_for("chapter_details", payload)
    local_tokens = int(counter(b"", messages))
    estimated_input = int(math.ceil(local_tokens * TOKEN_MARGIN_MULTIPLIER) + TOKEN_FRAMING_MARGIN)
    total_context = estimated_input + output_tokens + thinking_tokens
    return {
        "message_tokens": local_tokens,
        "estimated_input": estimated_input,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking_tokens,
        "total_context": total_context,
        "input_ok": estimated_input <= int(max_input),
        "total_ok": total_context < int(max_total),
        "fits": estimated_input <= int(max_input) and total_context < int(max_total),
    }


def _chapter_details_weight(counter: Callable[..., int], row: Mapping[str, Any]) -> int:
    encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=_json_default)
    return max(1, int(counter(b"", [{"role": "user", "content": encoded}])))


def _chapter_details_partitions(
    rows: Sequence[Mapping[str, Any]], weights: Sequence[int], count: int,
) -> list[list[dict[str, Any]]]:
    """Split ordered source rows into near-equal weighted complete-record batches."""

    target_count = max(1, min(int(count), len(rows)))
    total = sum(int(weight) for weight in weights)
    prefix = [0]
    for weight in weights:
        prefix.append(prefix[-1] + int(weight))
    output: list[list[dict[str, Any]]] = []
    start = 0
    for index in range(target_count):
        remaining_rows = len(rows) - start
        remaining_parts = target_count - index
        if remaining_rows <= 0:
            break
        if remaining_parts == 1:
            end = len(rows)
        else:
            target = total * (index + 1) / target_count
            end = start + 1
            best = abs(prefix[end] - target)
            for candidate in range(start + 1, len(rows) - (remaining_parts - 1) + 1):
                difference = abs(prefix[candidate] - target)
                if difference < best:
                    end, best = candidate, difference
        output.append([dict(row) for row in rows[start:end]])
        start = end
    if start < len(rows):
        output[-1].extend(dict(row) for row in rows[start:])
    return output


def _chapter_details_batch_payload(
    payload: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    index: int,
    count: int,
) -> dict[str, Any]:
    """Make one complete-record payload without dropping candidate material."""

    batch = json.loads(json.dumps(dict(payload), ensure_ascii=False, default=_json_default))
    batch_rows = [dict(row) for row in rows]
    batch["source_materials"] = batch_rows
    handles = {_text(row.get("source_handle")) for row in batch_rows if _text(row.get("source_handle"))}
    all_source_handles = {
        _text(row.get("source_handle"))
        for row in (payload.get("source_materials") or [])
        if isinstance(row, Mapping) and _text(row.get("source_handle"))
    }
    candidates = []
    for candidate in payload.get("candidate_materials") or []:
        if not isinstance(candidate, Mapping):
            continue
        handle = _text(candidate.get("source_handle"))
        if handle in handles or (index == 1 and (not handle or handle not in all_source_handles)):
            candidates.append(dict(candidate))
    batch["candidate_materials"] = candidates
    batch["chapter_details_batch"] = {
        "index": index,
        "count": count,
        "source_handles": sorted(handles),
        "complete_source_records": True,
        "not_full_chapter": count > 1,
    }
    return batch


def _chapter_details_adaptive_batches(
    planner: Any,
    payload: Mapping[str, Any],
    *,
    chapter_id: str = "chapter",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build dynamic source batches and exact-check every complete request."""

    counter = getattr(planner, "counter", None)
    if not callable(counter):
        raise ProgressivePlanError("chapter_details_capacity_counter_unavailable")
    rows = [dict(row) for row in payload.get("source_materials") or [] if isinstance(row, Mapping)]
    if not rows:
        raise ProgressivePlanError("chapter_details_fixed_context_exceeds_capacity")
    weights = [_chapter_details_weight(counter, row) for row in rows]
    base = _chapter_details_batch_payload(payload, [], index=1, count=1)
    base_capacity = _chapter_details_capacity(planner, base)
    if base_capacity is None or not base_capacity["fits"]:
        # A fixed envelope with no source records cannot be made smaller by
        # partitioning.  Stop before any paid request.
        raise ProgressivePlanError("chapter_details_fixed_context_exceeds_capacity")
    available = max(1, int((MAX_INPUT_TOKENS - TOKEN_FRAMING_MARGIN) / TOKEN_MARGIN_MULTIPLIER) - base_capacity["message_tokens"])
    initial_count = max(2, math.ceil(sum(weights) / available))
    initial_count = min(initial_count, len(rows))
    for count in range(initial_count, len(rows) + 1):
        partitions = _chapter_details_partitions(rows, weights, count)
        packages = [
            _chapter_details_batch_payload(payload, part, index=index, count=len(partitions))
            for index, part in enumerate(partitions, start=1)
        ]
        for index, package in enumerate(packages, start=1):
            package["call_id"] = f"chapter-details-batch:{_safe_id(chapter_id)}:{index}:{len(packages)}"
        estimates = [_chapter_details_capacity(planner, package) for package in packages]
        if all(estimate is not None and estimate["fits"] for estimate in estimates):
            return packages, [dict(estimate) for estimate in estimates if estimate is not None]
        if any(len(part) == 1 for part, estimate in zip(partitions, estimates) if estimate is not None and not estimate["fits"]):
            raise ProgressivePlanError("chapter_details_source_record_exceeds_capacity")
    raise ProgressivePlanError("chapter_details_source_batches_exceed_capacity")


def _chapter_details_cache_signature(payload: Mapping[str, Any], *, planner: Any = None) -> str:
    clean = dict(payload)
    clean.pop("call_id", None)
    model = _text(getattr(planner, "chapter_model", "")) or _text(getattr(planner, "model", ""))
    output_tokens = min(int(getattr(planner, "output_tokens", 16_000)), 16_000)
    messages = _messages_for("chapter_details", clean)
    return _material_content_signature({
        "contract": "chapter_details.batch.v2",
        "model": model,
        "output_tokens": output_tokens,
        "thinking_tokens": 2_048,
        "messages": messages,
    })


def _chapter_details_record_error(record: Any) -> str:
    """Check the adaptive output envelope, not its scientific completeness.

    A successful provider response is not necessarily a successful chapter.
    Keep this check off the ordinary unsplit path, and apply it to saved
    records as well as new responses so an old empty merge cannot resume.
    """

    if not isinstance(record, Mapping) or not isinstance(record.get("response"), Mapping):
        return "missing_chapter_plan"
    response = record["response"]
    plan = response.get("chapter_plan", response)
    if not isinstance(plan, Mapping) or not plan:
        return "missing_chapter_plan"
    telemetry = record.get("telemetry") if isinstance(record.get("telemetry"), Mapping) else {}
    for value in (record, response, plan, telemetry):
        status = _text(value.get("status")).casefold()
        if status in {"failed", "error", "partial", "unresolved", "incomplete", "blocked", "cancelled", "canceled"}:
            return "response_status:" + status
        if value.get("complete") is False:
            return "incomplete_response"
        if _text(value.get("finish_reason")).casefold() in {"length", "max_tokens", "max_output_tokens", "content_filter", "error"}:
            return "incomplete_response"
    units = _chapter_units(plan)
    if not units or any(not isinstance(unit, Mapping) for unit in units):
        return "missing_substantive_units"

    def has_content(value: Any) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, Mapping):
            return any(has_content(child) for key, child in value.items() if key not in {
                "unit_id", "id", "title", "source_handle", "source_handles", "source_ids", "paper_id", "paper_ids",
            })
        if isinstance(value, list):
            return any(has_content(child) for child in value)
        return False

    # Identifiers, headings and source inventories alone do not constitute a
    # substantive chapter.  This is a minimum content gate, not a quota or a
    # new semantic judge of whether the supplied findings are sufficient.
    if not any(has_content(unit.get(key)) for unit in units for key in (
        "substantive_point", "point", "claim", "development", "ordered_development", "paragraph_briefs",
    )):
        return "missing_substantive_units"
    return ""


def _chapter_details_failed_seam(
    cache_root: Path,
    *,
    seam: str,
    signature: str,
    chapter_id: str,
    error: Exception,
    successful_batches: Sequence[Mapping[str, Any]],
    record: Mapping[str, Any] | None = None,
    **details: Any,
) -> None:
    """Save the last failed attempt separately from every successful cache."""

    _atomic_json(cache_root / "failed_seams" / f"{seam}_{signature}.json", {
        "status": "failed",
        "seam": seam,
        "chapter_id": chapter_id,
        "cache_inputs": signature,
        "error": str(error),
        "error_type": type(error).__name__,
        "successful_batches": [dict(batch) for batch in successful_batches],
        **({"record": dict(record)} if record is not None else {}),
        **details,
    })


def _chapter_details_adaptive_record(
    planner: Any,
    payload: Mapping[str, Any],
    *,
    chapter_id: str,
    cache_root: Path,
    resume: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Call normal chapter_details directly or use cached adaptive batches."""

    capacity = _chapter_details_capacity(planner, payload)
    if capacity is None or capacity["fits"]:
        return _call_record(planner, "chapter_details", payload), None

    packages, estimates = _chapter_details_adaptive_batches(planner, payload, chapter_id=chapter_id)
    cache_root.mkdir(parents=True, exist_ok=True)
    batch_records: list[dict[str, Any]] = []
    batch_meta: list[dict[str, Any]] = []
    for index, (package, estimate) in enumerate(zip(packages, estimates), start=1):
        signature = _chapter_details_cache_signature(package, planner=planner)
        cache_path = cache_root / f"batch_{signature}.json"
        cached: Mapping[str, Any] | None = None
        if resume and cache_path.is_file():
            try:
                candidate = _read_json(cache_path)
                if (
                    isinstance(candidate, Mapping)
                    and candidate.get("status") == "complete"
                    and candidate.get("cache_inputs") == signature
                    and list(candidate.get("source_handles") or []) == [
                        _text(row.get("source_handle")) for row in package.get("source_materials") or []
                    ]
                    and not _chapter_details_record_error(candidate.get("record"))
                ):
                    cached = candidate
            except ProgressivePlanError:
                cached = None
        if cached is None:
            record = None
            try:
                record = _call_record(planner, "chapter_details", package)
                error = _chapter_details_record_error(record)
                if error:
                    raise ProgressivePlanError("chapter_details_batch_" + error)
            except Exception as exc:
                _chapter_details_failed_seam(
                    cache_root, seam="batch", signature=signature, chapter_id=chapter_id,
                    error=exc, record=record, successful_batches=batch_meta,
                    batch_index=index, batch_count=len(packages), estimate=estimate,
                    source_handles=[_text(row.get("source_handle")) for row in package.get("source_materials") or []],
                )
                raise
            _atomic_json(cache_path, {
                "status": "complete",
                "cache_inputs": signature,
                "chapter_id": chapter_id,
                "batch_index": index,
                "batch_count": len(packages),
                "source_handles": [_text(row.get("source_handle")) for row in package.get("source_materials") or []],
                "record": record,
            })
        else:
            record = dict(cached.get("record") or {})
        batch_records.append(record)
        batch_meta.append({
            "batch_index": index,
            "source_count": len(package.get("source_materials") or []),
            "source_handles": [_text(row.get("source_handle")) for row in package.get("source_materials") or []],
            "estimate": estimate,
            "cache_path": str(cache_path),
            "reused": cached is not None,
        })

    merge_payload = json.loads(json.dumps(dict(payload), ensure_ascii=False, default=_json_default))
    merge_payload["source_materials"] = []
    merge_payload["candidate_materials"] = []
    chapter_batches = []
    for index, (batch, record) in enumerate(zip(batch_meta, batch_records), start=1):
        response = _stage_response(record)
        chapter_plan = response.get("chapter_plan") if isinstance(response.get("chapter_plan"), Mapping) else response
        chapter_plan = json.loads(json.dumps(dict(chapter_plan), ensure_ascii=False, sort_keys=True, default=_json_default))
        chapter_batches.append({
            "batch_index": index,
            "source_handles": batch["source_handles"],
            "chapter_plan": dict(chapter_plan),
        })
    merge_payload["chapter_detail_batches"] = chapter_batches
    merge_payload["chapter_details_merge"] = {
        "batch_count": len(batch_records),
        "same_output_contract": "chapter_plan with thesis, reader_objective, units",
        "preserve_all_batch_units_and_source_handles": True,
        "do_not_invent_evidence": True,
    }
    merge_signature = _chapter_details_cache_signature(merge_payload, planner=planner)
    merge_payload["call_id"] = f"chapter-details-merge:{_safe_id(chapter_id)}:{merge_signature[:12]}"
    merge_capacity = _chapter_details_capacity(planner, merge_payload)
    if merge_capacity is None or not merge_capacity["fits"]:
        error = ProgressivePlanError("chapter_details_merge_context_preflight_exceeded")
        _chapter_details_failed_seam(
            cache_root, seam="merge", signature=merge_signature, chapter_id=chapter_id,
            error=error, successful_batches=batch_meta, estimate=merge_capacity,
        )
        raise error
    merge_cache = cache_root / f"merge_{merge_signature}.json"
    cached_merge: Mapping[str, Any] | None = None
    if resume and merge_cache.is_file():
        try:
            candidate = _read_json(merge_cache)
            if (
                isinstance(candidate, Mapping)
                and candidate.get("status") == "complete"
                and candidate.get("cache_inputs") == merge_signature
                and not _chapter_details_record_error(candidate.get("record"))
            ):
                cached_merge = candidate
        except ProgressivePlanError:
            cached_merge = None
    if cached_merge is None:
        merged_record = None
        try:
            merged_record = _call_record(planner, "chapter_details", merge_payload)
            error = _chapter_details_record_error(merged_record)
            if error:
                raise ProgressivePlanError("chapter_details_merge_" + error)
        except Exception as exc:
            _chapter_details_failed_seam(
                cache_root, seam="merge", signature=merge_signature, chapter_id=chapter_id,
                error=exc, record=merged_record, successful_batches=batch_meta, estimate=merge_capacity,
            )
            raise
        _atomic_json(merge_cache, {
            "status": "complete",
            "cache_inputs": merge_signature,
            "chapter_id": chapter_id,
            "record": merged_record,
        })
    else:
        merged_record = dict(cached_merge.get("record") or {})
    meta = {
        "mode": "adaptive_source_batches",
        "initial_capacity": capacity,
        "batch_count": len(batch_records),
        "batches": batch_meta,
        "merge_estimate": merge_capacity,
        "merge_cache_path": str(merge_cache),
        "merge_reused": cached_merge is not None,
    }
    return merged_record, meta


def _load_planner_json(content: str) -> Any:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        if not content.rstrip().endswith(("}", "]")):
            raise
        from json_repair import repair_json
        return repair_json(content, return_objects=True)


def _parse_planner_response(raw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(raw, Mapping) and "response" in raw and raw.get("_planner_call") is True:
        return dict(raw["response"]), dict(raw.get("telemetry") or {})
    if isinstance(raw, Mapping) and "content" in raw and isinstance(raw.get("content"), str):
        content = str(raw.get("content") or "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
        try:
            parsed = _load_planner_json(content)
        except json.JSONDecodeError as exc:
            raise ProgressivePlanError("planner_response_invalid_json") from exc
        if not isinstance(parsed, Mapping):
            raise ProgressivePlanError("planner_response_must_be_object")
        telemetry = {
            key: raw.get(key)
            for key in ("usage", "requested_model", "returned_model", "finish_reason", "complete", "call_id", "elapsed_seconds")
            if key in raw
        }
        return dict(parsed), telemetry
    if isinstance(raw, Mapping):
        return dict(raw), {}
    if isinstance(raw, str):
        content = raw.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
        try:
            parsed = _load_planner_json(content)
        except json.JSONDecodeError as exc:
            raise ProgressivePlanError("planner_response_invalid_json") from exc
        if isinstance(parsed, Mapping):
            return dict(parsed), {}
    raise ProgressivePlanError("planner_response_must_be_json_object")


class QwenProgressivePlanner:
    """Thin live adapter for the direct Model Studio Qwen client."""

    def __init__(
        self,
        *,
        model: str,
        key_file: str | Path,
        budget_ledger_path: str | Path,
        budget_limit_cny: float | None,
        output_dir: str | Path,
        tokenizer_path: str | Path = DEFAULT_TOKENIZER_PATH,
        timeout_seconds: float = 900.0,
        thinking_budget: int = 8_192,
        output_tokens: int = 18_000,
        chapter_model: str = DEFAULT_READER_MODEL,
    ):
        from .module4.runtime import GlobalBudgetLedger, QwenDirectClient

        self.ledger = GlobalBudgetLedger(path=Path(budget_ledger_path), limit_cny=budget_limit_cny)
        self.counter = qwen_local_token_counter(tokenizer_path)
        self.model = model
        self.output_dir = Path(output_dir)
        self.key_file = Path(key_file)
        self.timeout_seconds = timeout_seconds
        self.output_tokens = output_tokens
        self.thinking_budget = thinking_budget
        self.chapter_model = chapter_model

    def __call__(self, stage: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        from .module4.runtime import invoke_client
        from .module4.runtime import QwenDirectClient

        messages = _messages_for(stage, payload)
        call_model = self.chapter_model if stage in {"chapter_details", "case_groups"} else self.model
        output_tokens = self.output_tokens
        call_thinking_budget = 2_048 if stage in {"source_routing", "chapter_details", "case_groups"} else self.thinking_budget
        if stage == "chapter_details":
            output_tokens = min(output_tokens, 16_000)
        if stage == "whole_plan_improvement":
            output_tokens = min(output_tokens, 5_000)
            call_thinking_budget = min(call_thinking_budget, 1_024)
        elif stage == "affected_chapter_revision":
            output_tokens = min(output_tokens, 16_000)
            call_thinking_budget = min(call_thinking_budget, 1_024)
        estimated_input = int(self.counter(b"", messages) * TOKEN_MARGIN_MULTIPLIER + 0.999999) + TOKEN_FRAMING_MARGIN
        total_context_estimate = estimated_input + output_tokens + (call_thinking_budget if call_thinking_budget else 0)
        if estimated_input > MAX_INPUT_TOKENS or total_context_estimate >= 1_000_000:
            raise ProgressivePlanError(
                f"planner_context_preflight_exceeded:{stage}:input={estimated_input}:output={output_tokens}:thinking={call_thinking_budget}"
            )
        chapter = payload.get("chapter") if isinstance(payload.get("chapter"), Mapping) else {}
        identity = payload.get("call_id") or chapter.get("chapter_id") or payload.get("topic_id") or "global"
        call_id = "progressive-review:" + _safe_id(stage) + ":" + _safe_id(identity)
        client = QwenDirectClient(
            model=call_model,
            key_file=self.key_file,
            max_retries=1,
            timeout_seconds=self.timeout_seconds,
            max_output_tokens=output_tokens,
            thinking=True,
            thinking_budget=call_thinking_budget,
            json_mode=False,
            budget_ledger=self.ledger,
            raw_response_dir=self.output_dir / "raw_responses" / _safe_id(call_id),
            prompt_token_counter=self.counter,
            prompt_token_multiplier=TOKEN_MARGIN_MULTIPLIER,
            prompt_token_framing_margin=TOKEN_FRAMING_MARGIN,
        )
        raw = invoke_client(
            client,
            messages,
            model=call_model,
            max_output_tokens=output_tokens,
            thinking=True,
            thinking_budget=call_thinking_budget,
            call_id=call_id,
        )
        response, telemetry = _parse_planner_response(raw)
        telemetry["prompt_estimation"] = {
            "tokenizer": "data/tokenizers/qwen3_5_9b/tokenizer.json",
            "multiplier": TOKEN_MARGIN_MULTIPLIER,
            "framing_margin": TOKEN_FRAMING_MARGIN,
            "hosted_token_count_is_approximate": True,
        }
        return {"_planner_call": True, "response": response, "telemetry": telemetry}


def _call_record(planner: Callable[[str, Mapping[str, Any]], Any], stage: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    response, telemetry = _parse_planner_response(planner(stage, payload))
    return {"response": response, "telemetry": telemetry}


def _stage_response(record: Mapping[str, Any]) -> dict[str, Any]:
    response = record.get("response") if isinstance(record.get("response"), Mapping) else record
    return dict(response)


def _seed_owner_unit_ids(
    chapter_plan: Mapping[str, Any],
    arrangement: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Seed existing units with their arrangement IDs before owner revision.

    Older packets commonly omit IDs from the plan while the arrangement has
    stable IDs.  Seeding those known IDs gives an owner an explicit baseline;
    it does not infer identity for a newly split or merged unit.
    """

    seeded = json.loads(json.dumps(dict(chapter_plan), ensure_ascii=False, default=_json_default))
    units = seeded.get("units")
    if not isinstance(units, list):
        return seeded, []
    arranged_units = [item for item in arrangement.get("units") or () if isinstance(item, Mapping)]
    arranged_ids = [_text(item.get("unit_id") or item.get("id")) for item in arranged_units]
    has_explicit_relations = bool(_owner_unit_id_remap(arrangement)) or any(
        "source_briefs" in task for item in arranged_units
        for task in item.get("paragraph_tasks") or () if isinstance(task, Mapping)
    )
    def paragraph_ids(unit: Mapping[str, Any], key: str) -> set[str]:
        return {_text(item.get("paragraph_id")) for item in unit.get(key) or ()
                if isinstance(item, Mapping) and _text(item.get("paragraph_id"))}

    arranged_refs = []
    for item in arranged_units:
        refs: set[str] = set()
        for task in item.get("paragraph_tasks") or ():
            if not isinstance(task, Mapping):
                continue
            references = task.get("source_briefs") or [task.get("paragraph_id")]
            if isinstance(references, (str, Mapping)):
                references = [references]
            for ref in references:
                refs.add(_text(ref.get("paragraph_id") or ref.get("id")) if isinstance(ref, Mapping) else _text(ref))
        arranged_refs.append(refs)
    existing_ids: list[str] = []
    for index, unit in enumerate(units):
        if not isinstance(unit, Mapping):
            continue
        row = dict(unit)
        unit_id = _text(row.get("unit_id") or row.get("id"))
        if not unit_id:
            original_refs = paragraph_ids(row, "paragraph_briefs")
            matches = [arranged_id for arranged_id, refs in zip(arranged_ids, arranged_refs)
                       if arranged_id and original_refs and original_refs == refs]
            if len(matches) == 1:
                unit_id = matches[0]
            elif not original_refs and not has_explicit_relations and index < len(arranged_ids):
                # Compatibility only when neither side supplied a relationship.
                unit_id = arranged_ids[index]
            if unit_id:
                row["unit_id"] = unit_id
        if unit_id:
            existing_ids.append(unit_id)
        units[index] = row
    seeded["units"] = units
    return seeded, existing_ids


def build_arrangement_issue_revision_payload(
    *,
    chapter: Mapping[str, Any],
    chapter_plan: Mapping[str, Any],
    source_materials: Sequence[Mapping[str, Any]],
    arrangement: Mapping[str, Any],
    topic_id: str = "",
    research_question: str = "",
    candidate_navigation: Mapping[str, Any] | None = None,
    candidate_materials: Sequence[Mapping[str, Any]] = (),
    tool_materials: Sequence[Mapping[str, Any]] = (),
    feedback_materials: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build the existing chapter-owner input from arrangement feedback.

    The arrangement result remains an expression artifact.  Its issue list is
    converted into the same ``affected_chapter_revision`` payload used by the
    post-case planner, so a caller can rebuild the current task before asking
    arrangement or writing to proceed again.
    """

    chapter_id = _text(chapter.get("chapter_id") or chapter.get("id"))
    seeded_plan, existing_unit_ids = _seed_owner_unit_ids(chapter_plan, arrangement)
    issues = []
    for raw in arrangement.get("issues") or ():
        if not isinstance(raw, Mapping):
            continue
        issue = dict(raw)
        issue.setdefault("chapter_id", chapter_id)
        handles = issue.get("source_handles")
        if isinstance(handles, str):
            issue["source_handles"] = [handles]
        issues.append(issue)
    return {
        "topic_id": _text(topic_id),
        "research_question": _text(research_question),
        "planning_revision_mode": True,
        "call_id": f"arrangement-issue-revision-{_safe_id(chapter_id)}",
        "chapter_id": chapter_id,
        "chapter": dict(chapter),
        "chapter_plan": seeded_plan,
        "chapter_feedback": issues,
        "arrangement_issues": issues,
        "source_materials": [dict(item) for item in source_materials if isinstance(item, Mapping)],
        "candidate_navigation": dict(candidate_navigation or {}),
        "candidate_materials": [dict(item) for item in candidate_materials if isinstance(item, Mapping)],
        "tool_materials": [dict(item) for item in tool_materials if isinstance(item, Mapping)],
        "feedback_materials": [dict(item) for item in feedback_materials if isinstance(item, Mapping)],
        "unit_identity_contract": {
            "existing_unit_ids": existing_unit_ids,
            "new_unit_requires_explicit_unit_id": True,
            "split_or_merge_requires_unit_id_remap": True,
            "unit_id_remap_shape": "{new_unit_id: [old_unit_id, ...]}",
        },
        "required_behavior": {
            "return_complete_updated_plan": True,
            "review_original_and_incremental_material_together": True,
            "preserve_source_handles_and_limits": True,
            "treat_arrangement_issue_as_owner_request": True,
            "allow_no_material_change": True,
            "do_not_add_evidence": True,
        },
    }


def _feedback_action(issue: Mapping[str, Any]) -> str:
    """Normalize the small action vocabulary accepted by the feedback CLI."""

    raw = _text(issue.get("action") or issue.get("route") or issue.get("kind")).casefold()
    compact = raw.replace("-", "_").replace(" ", "_")
    if compact in {"local_backfill", "local_material_backfill", "backfill", "local"}:
        return "local_backfill"
    if compact in {"directed_read", "directed_reading", "deep_read", "deep_reading"}:
        return "directed_read"
    if compact in {"supplement", "supplement_read", "external_supplement", "retrieval"}:
        return "supplement"
    if compact in {"chapter_owner", "owner", "affected_chapter_revision", "chapter_revision", "omit", "exclude", "drop", ""}:
        return "chapter_owner"
    return compact


def _feedback_signature(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]


def _merge_feedback_source_materials(
    packet: Mapping[str, Any],
    incoming: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Merge authoritative local/tool material by handle without copying claims."""

    rows: dict[str, dict[str, Any]] = {
        _text(item.get("source_handle")): dict(item)
        for item in (packet.get("source_materials") or ())
        if isinstance(item, Mapping) and _text(item.get("source_handle"))
    }
    before = _feedback_signature({key: _material_content(value) for key, value in rows.items()})
    for raw in incoming:
        if not isinstance(raw, Mapping):
            continue
        handle = _text(raw.get("source_handle"))
        if not handle:
            continue
        current = rows.get(handle, {})
        if current and _owner_identity_conflict(current, raw):
            rows[handle] = {**current, "material_identity_conflict": True,
                            "conflicting_materials": [dict(raw)]}
            continue
        # A returned row is authoritative only for the fields it actually
        # supplied.  Old cases/findings are never copied into a new unit here.
        rows[handle] = {**current, **dict(raw)}
    after = _feedback_signature({key: _material_content(value) for key, value in rows.items()})
    return list(rows.values()), before != after


def _owner_unit_id_remap(value: Mapping[str, Any] | None) -> dict[str, list[str]]:
    """Read an explicit owner supplied new-unit -> old-unit map."""

    if not isinstance(value, Mapping):
        return {}
    raw = value.get("unit_id_remap") or value.get("unit_remap") or value.get("unit_id_mapping")
    if not isinstance(raw, Mapping):
        return {}
    output: dict[str, list[str]] = {}
    for new_id, old_ids in raw.items():
        key = _text(new_id)
        if not key:
            continue
        if isinstance(old_ids, str):
            old_ids = [old_ids]
        if isinstance(old_ids, Sequence):
            output[key] = list(dict.fromkeys(_text(item) for item in old_ids if _text(item)))
    return output


def _validate_owner_plan_update(
    old_plan: Mapping[str, Any],
    updated_plan: Mapping[str, Any],
    source_materials: Sequence[Mapping[str, Any]],
    *,
    owner_response: Mapping[str, Any] | None = None,
) -> tuple[dict[str, list[str]], list[str]]:
    """Validate structural changes without guessing identity by list position."""

    errors: list[str] = []
    old_units = [item for item in (old_plan.get("units") or ()) if isinstance(item, Mapping)]
    new_units = [item for item in (updated_plan.get("units") or ()) if isinstance(item, Mapping)]
    old_ids = [_text(item.get("unit_id") or item.get("id")) for item in old_units]
    new_ids = [_text(item.get("unit_id") or item.get("id")) for item in new_units]
    old_ids = [item for item in old_ids if item]
    new_ids = [item for item in new_ids if item]
    remap = _owner_unit_id_remap(owner_response) or _owner_unit_id_remap(updated_plan)
    if len(new_ids) != len(set(new_ids)):
        errors.append("updated_plan_duplicate_unit_ids")
    structural_change = len(old_units) != len(new_units) or (old_ids and set(old_ids) != set(new_ids))
    if structural_change:
        if len(new_ids) != len(new_units) or not remap:
            errors.append("structural_unit_change_requires_explicit_unit_ids_and_remap")
        else:
            allowed_old = set(old_ids)
            if not allowed_old:
                errors.append("structural_unit_change_old_units_missing_stable_ids")
            for new_id, old_refs in remap.items():
                if new_id not in set(new_ids) or any(old_id not in allowed_old for old_id in old_refs):
                    errors.append("unit_id_remap_references_unknown_unit")
            if any(new_id not in remap for new_id in set(new_ids) - allowed_old):
                errors.append("unit_id_remap_missing_new_unit")
            mapped_old = {old_id for old_refs in remap.values() for old_id in old_refs}
            if allowed_old - set(new_ids) - mapped_old:
                errors.append("unit_id_remap_missing_previous_unit")
    available = {
        _text(item.get("source_handle")) for item in source_materials
        if isinstance(item, Mapping) and _owner_material_has_content(item)
        and not item.get("material_identity_conflict")
    }
    for unit in new_units:
        handles = _owner_referenced_source_handles(unit)
        missing = sorted(handle for handle in handles if re.fullmatch(r"P\d{3,}", handle) and handle not in available)
        if missing:
            errors.append("updated_unit_sources_unavailable:" + ",".join(missing))
    return remap, list(dict.fromkeys(errors))


def _owner_referenced_source_handles(*values: Any) -> set[str]:
    """Collect explicit source handles from owner inputs, without reading prose."""

    handles: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key == "source_handle":
                    handle = _text(child)
                    if handle:
                        handles.add(handle)
                elif key == "source_handles":
                    items = [child] if isinstance(child, str) else child
                    if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
                        handles.update(_text(item) for item in items if _text(item))
                visit(child)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for child in value:
                visit(child)

    for value in values:
        visit(value)
    return {handle for handle in handles if handle}


def _owner_material_has_content(row: Mapping[str, Any]) -> bool:
    """Recognize supplied study substance, not identity, status or proposed use.

    This is a presence check, not a quality score. Review-derived findings have
    exactly the same admission path as the original paper's saved A/B.
    """
    from .practical_materials import has_practical_content
    if row.get("material_identity_conflict") or _owner_identity_conflict(row, row):
        return False
    aliases = {
        "usable_content": "content", "summary": "explanation", "summary_text": "explanation",
        "planning_summary": "explanation", "work_summary": "explanation",
        "key_findings": "finding", "findings": "finding", "key_finding": "finding",
        "approach": "details", "mechanisms": "details", "mechanism": "details",
        "contribution_and_limits": "details", "facet_contribution": "details",
        "scope": "details", "problem": "details", "methods": "details",
        "body_markdown": "content", "plain_text": "text",
    }
    ignored = {"sources", "source_identity_map", "paper_identity", "references", "bibliography",
               "source_handles", "title", "doi", "paper_id", "source_handle", "year",
               "proposed_use", "intended_use", "question", "read_focus", "still_missing",
               "interpretation_limits", "limits", "remaining_points", "use_in_review"}
    def normalize(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {aliases.get(str(k), str(k)): normalize(v) for k, v in value.items() if k not in ignored}
        if isinstance(value, (list, tuple)):
            return [normalize(v) for v in value]
        return value
    for key in ("study_summary_A", "review_planning_B", "deep_read_material", "local_passages",
                "supplement_gap_material", "supplement_gap_materials", "supplement_material", "supplement_materials",
                "tool_supplement_materials", "tool_materials", "material", "usable_content",
                "deep_read_materials", "local_passages_variants"):
        value = row.get(key)
        if isinstance(value, str):
            if value.strip():
                return True
        elif has_practical_content(normalize(value)):
            return True
    return False


def _owner_identity_conflict(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    identities = [{_text(row.get(key)) for key in ("paper_id", "canonical_paper_id") if _text(row.get(key))}
                  for row in (left, right)]
    if any(len(ids) > 1 for ids in identities) or (identities[0] and identities[1] and identities[0] != identities[1]):
        return True
    dois = [re.sub(r"^https?://(?:dx\.)?doi\.org/", "", _text(row.get("doi")).casefold())
            for row in (left, right)]
    return bool(dois[0] and dois[1] and dois[0] != dois[1])


def _resolve_owner_source_materials(
    *,
    source_materials: Sequence[Mapping[str, Any]],
    chapter_plan: Mapping[str, Any],
    chapter_feedback: Sequence[Mapping[str, Any]] = (),
    candidate_navigation: Mapping[str, Any] | None = None,
    candidate_materials: Sequence[Mapping[str, Any]] = (),
    pool_rows: Sequence[Mapping[str, Any]] = (),
    deep_material_by_paper: Mapping[str, Mapping[str, Any]] | None = None,
    tool_materials: Sequence[Mapping[str, Any]] = (),
    feedback_materials: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Close only selected sources from actual supplied channels, preserving provenance."""
    rows = {_text(item.get("source_handle")): dict(item) for item in source_materials
            if isinstance(item, Mapping) and _text(item.get("source_handle"))}
    referenced = _owner_referenced_source_handles(chapter_plan, chapter_feedback)
    report: dict[str, Any] = {"requested_handles": sorted(referenced), "resolved_from_current_pool": [],
                             "already_present": sorted(referenced & rows.keys()), "unresolved": []}
    candidates: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    def offer(channel: str, item: Mapping[str, Any]) -> None:
        handle = _text(item.get("source_handle"))
        if handle:
            normalized = dict(item)
            if not normalized.get("paper_id") and normalized.get("canonical_paper_id"):
                normalized["paper_id"] = normalized["canonical_paper_id"]
            candidates.setdefault(handle, []).append((channel, normalized))
    for item in candidate_materials:
        if isinstance(item, Mapping): offer("candidate_materials", item)
    for item in (candidate_navigation or {}).get("candidate_materials") or []:
        if isinstance(item, Mapping): offer("candidate_navigation", item)
    for item in feedback_materials:
        if isinstance(item, Mapping): offer("feedback_materials", item)
    for raw in pool_rows:
        if not isinstance(raw, Mapping): continue
        if _text(raw.get("_source_handle")) not in referenced | rows.keys():
            continue
        pid = _text(raw.get("_paper_id") or _canonical_paper_id(raw))
        local = build_local_material_payload(raw, deep_material=(deep_material_by_paper or {}).get(pid))
        for key in ("local_passages", "deep_read_material"):
            if raw.get(key): local[key] = raw[key]
        offer("current_pool", local)
    tool_identity_rows = {handle: dict(item) for handle, item in rows.items()}
    for handle, values in candidates.items():
        for _, item in values:
            known = tool_identity_rows.get(handle)
            if known is None:
                tool_identity_rows[handle] = dict(item)
            elif not _owner_identity_conflict(known, item):
                tool_identity_rows[handle] = {**dict(item), **known}
    resolved_tools = resolve_tool_materials({
        "source_materials": list(tool_identity_rows.values()), "tool_materials": list(tool_materials),
    })
    for tool in resolved_tools:
        if not isinstance(tool, Mapping): continue
        for identity in tool.get("sources") or []:
            if not isinstance(identity, Mapping): continue
            # A tool narrative is reusable with its cited identity, without
            # pretending the original study has its own downloaded A/B card.
            if not any(_text(identity.get(k)) for k in ("paper_id", "doi", "title")): continue
            offer("tool_materials", {**dict(identity), "tool_materials": [dict(tool)]})
    for handle in sorted(referenced | rows.keys()):
        current = rows.get(handle, {})
        channels = []
        conflict = False
        for channel, incoming in candidates.get(handle, []):
            if incoming.get("material_identity_conflict") or _owner_identity_conflict(current, incoming):
                report["unresolved"].append({"source_handle": handle, "reason": "source_identity_conflict",
                    "current_identity": {k: current.get(k) for k in ("paper_id", "doi", "title")},
                    "incoming_identity": {k: incoming.get(k) for k in ("paper_id", "doi", "title")}, "channel": channel})
                conflict = True
                continue
            if not _owner_material_has_content(incoming):
                continue
            merged = dict(current)
            for key, value in incoming.items():
                if key not in merged or merged[key] in (None, "", [], {}): merged[key] = value
            # Current substantive snapshots supersede older content only after
            # identity agreement; empty cards never erase usable review material.
            if channel == "current_pool":
                for key in ("study_summary_A", "review_planning_B", "supplement_gap_material", "supplement_gap_materials",
                            "supplement_material", "supplement_materials", "local_passages", "deep_read_material"):
                    if incoming.get(key): merged[key] = incoming[key]
            current = merged
            channels.append(channel)
        if current:
            if conflict: current["material_identity_conflict"] = True
            rows[handle] = current
        if handle in referenced and not conflict and not _owner_material_has_content(current):
            report["unresolved"].append({"source_handle": handle, "reason": "study_material_missing"})
        elif handle not in report["already_present"] and _owner_material_has_content(current) and not conflict:
            report["resolved_from_current_pool"].append({"source_handle": handle, "paper_id": current.get("paper_id"),
                                                       "material_channels": channels})
    return list(rows.values()), report


def _close_owner_response_materials(payload: Mapping[str, Any], response: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Adoption may use any material the owner actually received, never its invented findings."""
    return _resolve_owner_source_materials(
        source_materials=payload.get("source_materials") or [],
        chapter_plan=_owner_response_plan(response) or payload.get("chapter_plan") or {},
        candidate_materials=payload.get("candidate_materials") or [],
        candidate_navigation=payload.get("candidate_navigation") or {},
        tool_materials=payload.get("tool_materials") or [],
        feedback_materials=payload.get("feedback_materials") or [],
    )


def _owner_response_plan(response: Mapping[str, Any]) -> dict[str, Any] | None:
    """Extract one complete plan from the supported owner response shapes."""

    for entry in ProgressiveReviewPlanner._improvement_entries(response):
        candidate = ProgressiveReviewPlanner._improvement_plan(entry)
        if isinstance(candidate, Mapping) and candidate:
            return dict(candidate)
    candidate = response.get("updated_plan")
    return dict(candidate) if isinstance(candidate, Mapping) and candidate else None


def _classify_owner_response(
    old_plan: Mapping[str, Any],
    response: Mapping[str, Any],
    source_materials: Sequence[Mapping[str, Any]],
    *,
    expected_chapter_id: str = "",
) -> tuple[str, dict[str, Any] | None, dict[str, list[str]], list[str]]:
    """Validate owner output before a caller treats a provider call as complete."""

    response_status = _text(response.get("status") or response.get("revision_status")).casefold()
    remap: dict[str, list[str]] = {}
    errors: list[str] = []
    updated_plan = _owner_response_plan(response)
    if expected_chapter_id:
        owned_entries = [entry for entry in ProgressiveReviewPlanner._improvement_entries(response)
                         if isinstance(ProgressiveReviewPlanner._improvement_plan(entry), Mapping)]
        returned_ids = {_text(entry.get("chapter_id") or entry.get("id")) for entry in owned_entries}
        returned_ids.update(_text(value) for value in
                            (response.get("chapter_id"), (updated_plan or {}).get("chapter_id")))
        if any(value and value != expected_chapter_id for value in returned_ids):
            return "unresolved", None, remap, ["owner_response_chapter_mismatch"]
    if response_status in {"failed", "error", "partial", "unresolved", "incomplete"}:
        return "unresolved", None, remap, ["owner_response_status:" + response_status]
    if updated_plan is not None:
        remap, errors = _validate_owner_plan_update(
            old_plan, updated_plan, source_materials, owner_response=response,
        )
        if errors:
            return "unresolved", None, remap, errors
        if response_status in {"no_change", "unchanged", "no-change", "reused"}:
            return "no_change", updated_plan, remap, []
        return "updated", updated_plan, remap, []
    if response_status in {"no_change", "unchanged", "no-change", "reused"}:
        return "no_change", dict(old_plan), remap, []
    return "unresolved", None, remap, ["owner_response_missing_complete_plan"]


def _canonicalize_feedback_materials(
    packet: Mapping[str, Any],
    incoming: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Attach local handles to raw supplement/deep material before owner use."""

    rows = [dict(item) for item in (packet.get("source_materials") or ()) if isinstance(item, Mapping)]
    rows.extend(dict(item) for item in (packet.get("candidate_materials") or ()) if isinstance(item, Mapping))
    rows.extend({**dict(item), "source_handle": handle} for handle, item in
                (packet.get("source_identity_map") or {}).items() if isinstance(item, Mapping))
    by_paper = {
        _text(item.get("paper_id") or item.get("canonical_paper_id")): _text(item.get("source_handle"))
        for item in rows
        if _text(item.get("paper_id") or item.get("canonical_paper_id")) and _text(item.get("source_handle"))
    }
    used = {
        int(match.group(1))
        for match in (re.fullmatch(r"P(\d+)", _text(item.get("source_handle"))) for item in rows)
        if match
    }
    next_number = max(used or {0}) + 1
    output: list[dict[str, Any]] = []
    for raw in incoming:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        identity = row.get("record_identity") if isinstance(row.get("record_identity"), Mapping) else {}
        paper_id = _text(row.get("paper_id") or row.get("canonical_paper_id") or identity.get("paper_id") or identity.get("canonical_paper_id"))
        handle = by_paper.get(paper_id, "") or _text(row.get("source_handle"))
        if not handle and paper_id:
            handle = f"P{next_number:04d}"
            next_number += 1
            by_paper[paper_id] = handle
        if handle:
            row["source_handle"] = handle
        if paper_id and not _text(row.get("paper_id")):
            row["paper_id"] = paper_id
        output.append(row)
    return output


def run_feedback_loop(
    *,
    packet_path: str | Path,
    arrangement_path: str | Path,
    arrangement: Mapping[str, Any],
    owner_planner: Callable[[str, Mapping[str, Any]], Any],
    arrangement_runner: Callable[[Mapping[str, Any], Mapping[str, Any], Path], Mapping[str, Any]],
    writer_runner: Callable[[Mapping[str, Any], Mapping[str, Any], Path], Mapping[str, Any]],
    output_dir: str | Path,
    feedback_runner: Callable[[Sequence[Mapping[str, Any]], Mapping[str, Any], Path], Mapping[str, Any]] | None = None,
    resume: bool = True,
    execution_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Consume one arrangement feedback envelope and rebuild downstream inputs.

    The callbacks are the existing arrangement and writer entry points supplied
    by the CLI; this function only sequences them and persists the authoritative
    owner plan.  Empty feedback reuses the current packet and downstream
    artifacts without issuing a new model call.
    """

    packet_file = Path(packet_path)
    arrangement_file = Path(arrangement_path)
    packet = _read_json(packet_file)
    if not isinstance(packet, Mapping):
        raise ProgressivePlanError("feedback_packet_must_be_object")
    arrangement_value = dict(arrangement)
    issues = [dict(item) for item in (arrangement_value.get("issues") or ()) if isinstance(item, Mapping)]

    def pending_arrangement(value: Mapping[str, Any]) -> dict[str, Any]:
        # Consume the existing validator's verdict, not unit count as proof of
        # completion. Legacy callbacks that omit validation remain compatible;
        # a supplied verdict must affirm success and contain no contrary signal.
        validation = value.get("validation")
        status = _text(validation.get("status")).casefold() if isinstance(validation, Mapping) else ""
        incomplete = not value.get("units")
        if "validation" in value:
            incomplete = incomplete or not isinstance(validation, Mapping) or (
                validation.get("ok") is False
                or validation.get("contract_ok") is False
                or validation.get("needs_arrangement") is True
                or bool(validation.get("errors"))
                or bool(validation.get("sources_never_mentioned") or validation.get("missing_sources"))
                or (bool(status) and status != "arranged")
                or not (validation.get("ok") is True or status == "arranged")
            )
        if not incomplete:
            return {}
        return {
            "status": "partial",
            "pending_arrangement": True,
            "arrangement_status": status or "partial",
            "arrangement_validation": validation,
            "writer": "",
        }

    original_pending = pending_arrangement(arrangement_value) if "validation" in arrangement_value else {}
    if not issues:
        return {
            "status": "reused",
            "updated_packet": str(packet_file),
            "arrangement": str(arrangement_file),
            "writer": "",
            "issues": [],
            **original_pending,
        }
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    active_path = target_dir / "FEEDBACK_ACTIVE.json"
    previous_pointer: Mapping[str, Any] = {}
    if active_path.is_file():
        try:
            loaded_pointer = _read_json(active_path)
            if isinstance(loaded_pointer, Mapping):
                previous_pointer = loaded_pointer
        except ProgressivePlanError:
            previous_pointer = {}
    # Probe only local card/A/B content before deciding whether to call a tool
    # runner.  This keeps identical retries from repeating paid actions while
    # still invalidating the cache when a same-handle card changed.
    probe_packet = dict(packet)
    previous_artifact = Path(str(previous_pointer.get("artifact_dir") or ""))
    previous_packet_path = previous_artifact / "UPDATED_WRITER_PACKET.json"
    if previous_packet_path.is_file():
        try:
            previous_packet = _read_json(previous_packet_path)
        except ProgressivePlanError:
            previous_packet = {}
        if isinstance(previous_packet, Mapping):
            merged_previous, _ = _merge_feedback_source_materials(probe_packet, previous_packet.get("source_materials") or [])
            probe_packet["source_materials"] = merged_previous
            if isinstance(previous_packet.get("tool_materials"), list):
                probe_packet["tool_materials"] = list(previous_packet.get("tool_materials") or [])
            if isinstance(previous_packet.get("feedback_materials"), list):
                probe_packet["feedback_materials"] = list(previous_packet.get("feedback_materials") or [])
    probe_sources = [dict(item) for item in (probe_packet.get("source_materials") or ()) if isinstance(item, Mapping)]
    if probe_sources:
        refreshed_probe = _refresh_local_material_snapshots([{"source_materials": probe_sources}])[0]
        probe_packet["source_materials"] = refreshed_probe.get("source_materials") or probe_sources
    # Downstream packets always serialize these optional lists, while the
    # original packet may omit them.  Treat absent and empty as the same probe
    # input so a completed no-tool run can resume without a duplicate call.
    for optional_key in ("tool_materials", "feedback_materials"):
        if not probe_packet.get(optional_key):
            probe_packet.pop(optional_key, None)
    owner_issues = [item for item in issues if _feedback_action(item) == "chapter_owner"]
    action_issues = [item for item in issues if _feedback_action(item) != "chapter_owner"]
    resume_signature = _feedback_signature({
        "packet": probe_packet,
        "issues": issues,
        "execution_context": dict(execution_context or {}),
    })
    # Keep this probe identity separate from the downstream artifact identity.
    # A completed run must be reusable when its local inputs are unchanged;
    # changing a saved card changes this probe and therefore reopens the loop.
    resume_probe_signature = resume_signature
    previous_status = _text(previous_pointer.get("status")).casefold()
    reusable_statuses = {"complete", "completed", "updated", "updated_material", "reused", "no_change"}
    if resume and previous_pointer.get("resume_signature") == resume_signature and (
        previous_status in reusable_statuses or previous_pointer.get("pending_arrangement") is True
    ):
        previous_dir = Path(str(previous_pointer.get("artifact_dir") or ""))
        cached_outputs = (
            previous_dir / "UPDATED_WRITER_PACKET.json",
            previous_dir / "CHAPTER_ARRANGEMENT.json",
            previous_dir / "WRITTEN_RESULT.json",
        )
        if cached_outputs[0].is_file() and cached_outputs[1].is_file():
            cached_arrangement = _read_json(cached_outputs[1])
            pending = pending_arrangement(cached_arrangement) if isinstance(cached_arrangement, Mapping) else {
                "status": "partial", "pending_arrangement": True,
                "arrangement_status": "invalid_arrangement", "arrangement_validation": None, "writer": "",
            }
            if pending:
                # Older completed pointers may have been written after an
                # invalid arrangement. Keep their artifacts, but never reuse
                # that pointer as completed or restart paid callbacks just to
                # report its existing pending work.
                if _read_json(active_path) != previous_pointer:
                    raise ProgressivePlanError("feedback_result_stale_active_artifact")
                _atomic_json(active_path, {**dict(previous_pointer), **pending})
                return {
                    **pending,
                    "updated_packet": str(cached_outputs[0]),
                    "arrangement": str(cached_outputs[1]),
                    "issues": issues,
                    "owner_status": previous_pointer.get("owner_status") or "",
                    "input_signature": previous_pointer.get("input_signature") or resume_signature,
                    "resume_signature": resume_signature,
                    "reuse_reason": "identical_feedback_pending_arrangement",
                }
        if all(path.is_file() for path in cached_outputs):
            return {
                "status": "reused",
                "updated_packet": str(cached_outputs[0]),
                "arrangement": str(cached_outputs[1]),
                "writer": str(cached_outputs[2]),
                "issues": issues,
                "input_signature": previous_pointer.get("input_signature") or resume_signature,
                "resume_signature": resume_signature,
                "reuse_reason": "identical_feedback_and_material_inputs",
            }
        if previous_status in {"reused", "no_change"} or (
            previous_pointer.get("pending_arrangement") is True and original_pending
        ):
            if original_pending:
                if _read_json(active_path) != previous_pointer:
                    raise ProgressivePlanError("feedback_result_stale_active_artifact")
                _atomic_json(active_path, {**dict(previous_pointer), **original_pending})
            return {
                "status": "reused",
                "updated_packet": str(packet_file),
                "arrangement": str(arrangement_file),
                "writer": "",
                "issues": issues,
                "resume_signature": resume_signature,
                "reuse_reason": "owner_no_change_and_material_inputs_unchanged",
                **original_pending,
            }
    _atomic_json(active_path, {
        "resume_signature": resume_signature,
        "status": "in_progress",
        "artifact_dir": str(target_dir / (resume_signature + ".staging")),
    })
    staging_dir = target_dir / (resume_signature + ".staging")
    staging_dir.mkdir(parents=True, exist_ok=True)
    working_packet = dict(packet)
    action_result: dict[str, Any] = {}
    material_changed = False
    tool_materials: list[dict[str, Any]] = []
    feedback_materials: list[dict[str, Any]] = []
    if action_issues:
        if feedback_runner is None:
            _atomic_json(active_path, {
                "resume_signature": resume_probe_signature,
                "status": "partial",
                "artifact_dir": str(staging_dir),
                "unhandled_actions": [_feedback_action(item) for item in action_issues],
            })
            return {
                "status": "partial",
                "updated_packet": str(packet_file),
                "arrangement": str(arrangement_file),
                "writer": "",
                "issues": issues,
                "unhandled_actions": [_feedback_action(item) for item in action_issues],
            }
        raw_action_result = feedback_runner(action_issues, packet, staging_dir)
        action_result = dict(raw_action_result or {}) if isinstance(raw_action_result, Mapping) else {
            "status": "partial", "error": "feedback_runner_return_not_object",
        }
        incoming = _canonicalize_feedback_materials(
            working_packet, action_result.get("source_materials") or (),
        )
        merged_sources, material_changed = _merge_feedback_source_materials(working_packet, incoming)
        if incoming:
            working_packet["source_materials"] = merged_sources
        tool_materials = _canonicalize_feedback_materials(
            working_packet, action_result.get("tool_materials") or (),
        )
        feedback_materials = _canonicalize_feedback_materials(
            working_packet, action_result.get("feedback_materials") or (),
        )
        before_tool_materials = packet.get("tool_materials") or []
        before_feedback_materials = packet.get("feedback_materials") or []
        if tool_materials:
            working_packet["tool_materials"] = [
                *[dict(item) for item in (working_packet.get("tool_materials") or ()) if isinstance(item, Mapping)],
                *tool_materials,
            ]
        if feedback_materials:
            working_packet["feedback_materials"] = [
                *[dict(item) for item in (working_packet.get("feedback_materials") or ()) if isinstance(item, Mapping)],
                *feedback_materials,
            ]
        material_changed = material_changed or _feedback_signature({
            "tool_materials": before_tool_materials,
            "feedback_materials": before_feedback_materials,
        }) != _feedback_signature({
            "tool_materials": working_packet.get("tool_materials") or [],
            "feedback_materials": working_packet.get("feedback_materials") or [],
        })
        owner_issues.extend(dict(item) for item in (action_result.get("owner_feedback") or ()) if isinstance(item, Mapping))
        if material_changed and not owner_issues:
            owner_issues.append({
                "action": "chapter_owner",
                "problem": "Review the updated local material for this chapter and retain or revise the plan with its actual conditions and limits.",
                "source_handles": [
                    _text(item.get("source_handle")) for item in incoming
                    if _text(item.get("source_handle"))
                ],
            })
    if not owner_issues:
        unresolved_actions = bool(
            action_issues and (
                action_result.get("status") not in {"fulfilled", "complete", "completed", "reused", "local_material_ready"}
                or action_result.get("unmet_actions")
            )
        )
        _atomic_json(active_path, {
            "resume_signature": resume_probe_signature,
            "status": "partial" if unresolved_actions else "reused",
            "artifact_dir": str(staging_dir),
            **(original_pending if not unresolved_actions else {}),
        })
        return {
            "status": "partial" if unresolved_actions else "reused",
            "updated_packet": str(packet_file),
            "arrangement": str(arrangement_file),
            "writer": "",
            "issues": issues,
            "action_result": action_result,
            **original_pending,
        }
    chapter = working_packet.get("chapter") if isinstance(working_packet.get("chapter"), Mapping) else {}
    chapter_plan = working_packet.get("chapter_plan") if isinstance(working_packet.get("chapter_plan"), Mapping) else {}
    # The signature includes fresh local/tool material, so a same-handle card
    # change invalidates a previous loop even when the thesis is unchanged.
    input_signature = _feedback_signature({
        "packet": working_packet,
        "issues": owner_issues,
        "actions": action_issues,
        "action_status": action_result.get("status"),
        "material_changed": material_changed,
        "tool_materials": tool_materials,
        "feedback_materials": feedback_materials,
        "execution_context": dict(execution_context or {}),
    })
    artifact_dir = target_dir / input_signature
    artifact_dir.mkdir(parents=True, exist_ok=True)
    outputs = (
        artifact_dir / "UPDATED_WRITER_PACKET.json",
        artifact_dir / "CHAPTER_ARRANGEMENT.json",
        artifact_dir / "WRITTEN_RESULT.json",
    )
    _atomic_json(active_path, {
        "resume_signature": resume_probe_signature,
        "input_signature": input_signature,
        "artifact_dir": str(artifact_dir),
        "status": "in_progress",
    })

    def assert_active() -> None:
        current = _read_json(active_path)
        if not isinstance(current, Mapping) or current.get("input_signature") != input_signature or current.get("artifact_dir") != str(artifact_dir):
            raise ProgressivePlanError("feedback_result_stale_active_artifact")

    config = ProgressivePlannerConfig(
        topic_id="feedback-loop",
        pool_path=packet_file,
        plan_path=packet_file,
        output_dir=artifact_dir,
        planning_revision_enabled=True,
        shared_deep_read_budget=0,
        chapter_workers=1,
    )
    planner = ProgressiveReviewPlanner(config, planner=owner_planner)
    owner_result = planner.revise_from_arrangement_issues(
        chapter=chapter,
        chapter_plan=chapter_plan,
        source_materials=working_packet.get("source_materials") or [],
        arrangement={**arrangement_value, "issues": owner_issues},
        research_question=_text(working_packet.get("research_question")),
        candidate_navigation=working_packet.get("candidate_navigation") if isinstance(working_packet.get("candidate_navigation"), Mapping) else None,
        candidate_materials=working_packet.get("candidate_materials") or [],
        tool_materials=working_packet.get("tool_materials") or tool_materials,
        feedback_materials=working_packet.get("feedback_materials") or feedback_materials,
    )
    updated_plan = owner_result.get("updated_plan")
    owner_status = _text(owner_result.get("status"))
    if not isinstance(updated_plan, Mapping):
        assert_active()
        _atomic_json(active_path, {
            "input_signature": input_signature, "status": "partial",
            "resume_signature": resume_probe_signature, "artifact_dir": str(artifact_dir),
            "owner_status": owner_status, "structural_errors": owner_result.get("structural_errors") or [],
        })
        return {
            "status": "partial",
            "updated_packet": str(packet_file),
            "arrangement": str(arrangement_file),
            "writer": "",
            "issues": issues,
            "owner_status": owner_status or "unresolved",
            "owner_payload": owner_result.get("owner_payload") or {},
            "structural_errors": owner_result.get("structural_errors") or [],
            "action_result": action_result,
        }
    unresolved_actions = bool(
        action_issues and (
            action_result.get("status") not in {"fulfilled", "complete", "completed", "reused", "local_material_ready"}
            or action_result.get("unmet_actions")
        )
    )
    if owner_status == "no_change" and not material_changed:
        assert_active()
        status = "partial" if unresolved_actions else "reused"
        _atomic_json(active_path, {"input_signature": input_signature, "resume_signature": resume_probe_signature,
                                  "artifact_dir": str(artifact_dir), "status": status, "owner_status": "no_change",
                                  **(original_pending if not unresolved_actions else {})})
        return {
            "status": status,
            "updated_packet": str(packet_file),
            "arrangement": str(arrangement_file),
            "writer": "",
            "issues": issues,
            "owner_status": "no_change",
            "action_result": action_result,
            **original_pending,
        }
    assert_active()
    updated_packet = dict(working_packet)
    updated_packet["chapter_plan"] = dict(updated_plan)
    adopted = (owner_result.get("rebuild_arrangement_input") or {}).get("source_materials")
    if adopted is not None:
        updated_packet["source_materials"] = adopted
        identity_map = dict(updated_packet.get("source_identity_map") or {})
        for material in adopted:
            if material.get("source_handle"):
                identity_map[material["source_handle"]] = {**identity_map.get(material["source_handle"], {}),
                    **{k: material.get(k) for k in ("paper_id", "title", "doi", "year") if material.get(k)}}
        updated_packet["source_identity_map"] = identity_map
    updated_packet["feedback_issues"] = [dict(item) for item in issues]
    updated_packet["feedback_action_result"] = action_result
    updated_packet["feedback_materials"] = feedback_materials
    updated_packet["unit_id_remap"] = owner_result.get("unit_id_remap") or {}
    updated_packet_path = artifact_dir / "UPDATED_WRITER_PACKET.json"
    _atomic_json(updated_packet_path, updated_packet)
    assert_active()
    rebuilt_arrangement = dict(arrangement_runner(updated_packet, {**arrangement_value, "issues": owner_issues}, artifact_dir))
    rebuilt_arrangement_path = artifact_dir / "CHAPTER_ARRANGEMENT.json"
    _atomic_json(rebuilt_arrangement_path, rebuilt_arrangement)
    pending = pending_arrangement(rebuilt_arrangement)
    if pending:
        assert_active()
        # Adopted material is already persisted in the updated packet. Match
        # the next local probe so unchanged pending work does not repeat paid
        # callbacks, including when the owner adopted a supplied candidate.
        pending_probe = dict(probe_packet)
        pending_sources, _ = _merge_feedback_source_materials(pending_probe, updated_packet.get("source_materials") or [])
        if pending_sources:
            pending_probe["source_materials"] = _refresh_local_material_snapshots(
                [{"source_materials": pending_sources}])[0].get("source_materials") or pending_sources
        for key in ("tool_materials", "feedback_materials"):
            if updated_packet.get(key): pending_probe[key] = updated_packet[key]
            else: pending_probe.pop(key, None)
        resume_probe_signature = _feedback_signature({"packet": pending_probe, "issues": issues,
                                                    "execution_context": dict(execution_context or {})})
        _atomic_json(active_path, {"input_signature": input_signature, "resume_signature": resume_probe_signature,
                                  "artifact_dir": str(artifact_dir), "owner_status": owner_status, **pending})
        return {
            **pending,
            "updated_packet": str(updated_packet_path),
            "arrangement": str(rebuilt_arrangement_path),
            "issues": issues,
            "owner_status": owner_status,
            "action_result": action_result,
            "input_signature": input_signature,
            "resume_signature": resume_probe_signature,
        }
    assert_active()
    written = dict(writer_runner(updated_packet, rebuilt_arrangement, artifact_dir))
    writer_path = artifact_dir / "WRITTEN_RESULT.json"
    _atomic_json(writer_path, written)
    body = written.get("body_markdown")
    if isinstance(body, str):
        assert_active()
        (artifact_dir / "WRITTEN_BODY.md").write_text(body.rstrip() + "\n", encoding="utf-8")
    # Consume the REAL writer result shape: a body being present never means
    # the problem is resolved.  An explicit incomplete flag, a length cutoff,
    # new writer-issued issues, or a partial multi-unit dispatch all keep the
    # run partial and surface the pending work in the final report.
    written_issues = [
        dict(item) for item in (written.get("issues") or [])
        if isinstance(item, Mapping)
    ]
    writer_complete = written.get("complete")
    writer_completion = _text(written.get("completion_status"))
    pending_units = [
        _text(item) for item in (written.get("affected_units") or [])
        if _text(item)
    ] if _text(written.get("status")).casefold() == "partial" else []
    unresolved_writer = (
        writer_complete is False
        or writer_completion in {"partial_length", "partial", "unresolved"}
        or bool(written_issues)
        or bool(pending_units)
    )
    final_status = "partial" if (
        action_result.get("status") in {"partial", "unmet", "failed", "external_research_required"}
        or _text(written.get("status")).casefold() in {"partial", "unresolved", "failed"}
        or unresolved_writer
        or (not isinstance(written.get("body_markdown"), str) and not written.get("body_path"))
    ) else ("updated_material" if material_changed and owner_status == "no_change" else owner_status or "updated")
    # Candidate adoption adds real rows to the persisted packet. The next
    # replay probes those same rows; fingerprint that material-complete input
    # now so an identical request does not require a second owner call.
    completed_probe = dict(probe_packet)
    completed_sources, _ = _merge_feedback_source_materials(completed_probe, updated_packet.get("source_materials") or [])
    if completed_sources:
        completed_probe["source_materials"] = _refresh_local_material_snapshots(
            [{"source_materials": completed_sources}])[0].get("source_materials") or completed_sources
    for key in ("tool_materials", "feedback_materials"):
        if updated_packet.get(key): completed_probe[key] = updated_packet[key]
        else: completed_probe.pop(key, None)
    resume_probe_signature = _feedback_signature({"packet": completed_probe, "issues": issues,
                                                "execution_context": dict(execution_context or {})})
    assert_active()
    _atomic_json(active_path, {"input_signature": input_signature, "resume_signature": resume_probe_signature, "artifact_dir": str(artifact_dir), "status": final_status, "owner_status": owner_status})
    return {
        "status": final_status,
        "updated_packet": str(updated_packet_path),
        "arrangement": str(rebuilt_arrangement_path),
        "writer": str(writer_path),
        "issues": [dict(item) for item in issues if isinstance(item, Mapping)],
        # Honest bookkeeping from the real writer result, for the caller's
        # final report; empty lists mean nothing is pending.
        "writer_complete": None if writer_complete is None else bool(writer_complete),
        "writer_completion": writer_completion,
        "writer_issues": written_issues,
        "pending_units": pending_units,
        "owner_payload": owner_result.get("owner_payload") or {},
        "action_result": action_result,
        "input_signature": input_signature,
        "resume_signature": resume_probe_signature,
        "unit_id_remap": owner_result.get("unit_id_remap") or {},
    }


def _merge_directed_tasks(tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for raw in tasks:
        paper_id = _text(raw.get("paper_id") or raw.get("canonical_paper_id"))
        if not paper_id:
            continue
        requirements = _directed_task_requirements(raw)
        task_key = paper_id + ":" + _directed_task_signature(raw)
        raw = {**dict(raw), **requirements}
        row = merged.setdefault(task_key, {
            "paper_id": paper_id,
            "chapter_ids": [],
            "questions": [],
            "required_outputs": [],
            "knowledge_gaps": [],
            "reasons": [],
            "priority": raw.get("priority", 0),
        })
        if raw.get("retry_empty_result") is True:
            row["retry_empty_result"] = True
        if raw.get("round_specs") and not row.get("round_specs"):
            row["round_specs"] = [dict(spec) for spec in raw["round_specs"] if isinstance(spec, Mapping)]
        owners = raw.get("chapter_ids") or []
        if isinstance(owners, str):
            owners = [owners]
        if raw.get("chapter_id"):
            owners = [*owners, raw["chapter_id"]]
        row["chapter_ids"] = list(dict.fromkeys([*row["chapter_ids"], *[str(x) for x in owners if _text(x)]]))
        for key in ("questions", "required_outputs"):
            values = raw.get(key) or ((raw.get("question") or raw.get("focus")) if key == "questions" else None) or []
            if isinstance(values, (str, Mapping)):
                values = [values]
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                existing = {json.dumps(item, ensure_ascii=False, sort_keys=True, default=_json_default) for item in row[key]}
                for item in values:
                    normalized = dict(item) if isinstance(item, Mapping) else ({("question" if key == "questions" else "description"): str(item)} if _text(item) else None)
                    if normalized is not None:
                        encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=_json_default)
                        if encoded not in existing:
                            row[key].append(normalized)
                            existing.add(encoded)
        for key, value in (("knowledge_gaps", raw.get("knowledge_gap")), ("reasons", raw.get("reason") or raw.get("expected_information_gain"))):
            if _text(value) and _text(value) not in row[key]:
                row[key].append(_text(value))
        for key in ("knowledge_gaps", "reasons"):
            values = raw.get(key) or []
            if isinstance(values, str):
                values = [values]
            row[key] = list(dict.fromkeys([*row[key], *[_text(value) for value in values if _text(value)]]))
        try:
            row["priority"] = max(float(row.get("priority") or 0), float(raw.get("priority") or 0))
        except (TypeError, ValueError):
            pass
    return list(merged.values())


def _normalize_gaps(gaps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    from .planning_material_triage import normalize_intended_use

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(gaps, start=1):
        gap_id = _safe_id(raw.get("gap_id") or raw.get("id"), f"gap_{index:02d}")
        if gap_id in seen:
            continue
        seen.add(gap_id)
        question = _text(raw.get("gap_question") or raw.get("question"))
        if not question:
            continue
        queries = []
        raw_queries = raw.get("targeted_queries") or raw.get("queries") or []
        if isinstance(raw_queries, str):
            raw_queries = [raw_queries]
        for item in raw_queries:
            if isinstance(item, Mapping):
                query = _text(item.get("query_text") or item.get("query") or item.get("text"))
                kind = _text(item.get("query_type") or item.get("type") or "keyword").casefold()
                facet_id = _text(item.get("facet_id") or "F1")
            else:
                query = _text(item)
                kind = "keyword"
                facet_id = "F1"
            if query and kind in {"keyword", "question"}:
                queries.append({"query_text": query, "query_type": kind, "facet_id": facet_id})
        raw_criteria = raw.get("success_criteria") or []
        if isinstance(raw_criteria, str):
            raw_criteria = [raw_criteria]
        reuse_facets = raw.get("reuse_plan_facet_ids") or raw.get("optional_reuse_plan_facet_ids") or []
        if isinstance(reuse_facets, str):
            reuse_facets = [reuse_facets]
        criteria = [str(x).strip() for x in raw_criteria if _text(x)]
        round_specs = [dict(item) for item in (raw.get("round_specs") or ()) if isinstance(item, Mapping)]
        if not round_specs and queries:
            round_specs = [{"round": 1, "targeted_queries": queries[:3]}]
        normalized.append({
            "gap_id": gap_id,
            "gap_question": question,
            "success_criteria": criteria,
            "required_outputs": _supplement_requirement_rows(raw.get("required_outputs")),
            "targeted_queries": queries[:3],
            "round_specs": round_specs or ([{"round": 1, "targeted_queries": queries[:3]}] if queries else []),
            "reuse_plan_facet_ids": list(dict.fromkeys(_text(x) for x in reuse_facets if _text(x)))[:3],
            "known_papers": [dict(x) if isinstance(x, Mapping) else {"paper_id": str(x)} for x in (raw.get("known_papers") or []) if isinstance(x, Mapping) or _text(x)],
            "reviewed_references": [dict(x) for x in (raw.get("reviewed_references") or []) if isinstance(x, Mapping)],
            "chapter_ids": list(dict.fromkeys(_text(x) for x in (raw.get("chapter_ids") or []) if _text(x))),
            "priority": raw.get("priority", 0),
            **{key: raw[key] for key in ("intended_use", "user_scope", "required_concepts", "known_paper_handles", "comparison_object", "need_id", "retry_empty_result") if key in raw},
        })
        if not normalized[-1]["success_criteria"]:
            normalized[-1]["success_criteria"] = ["Return directly relevant evidence or state why the gap remains unresolved."]
        if "intended_use" in normalized[-1]:
            # A mixed label describes one task, not an unsupported tool route.
            # Quantitative and application-oriented reading retains the more capable reader.
            uses = {
                normalize_intended_use(use)
                for use in re.split(r"[^a-z_]+", _text(raw.get("intended_use")).casefold())
            }
            normalized[-1]["intended_use"] = next((use for use in (
                "quantification", "application_outcome", "comparison", "mechanism", "study_design", "background"
            ) if use in uses), "mechanism")
    return normalized


def _all_directed_ids(record: Mapping[str, Any]) -> set[str]:
    output: set[str] = {_text(value) for value in record.get("consumed_paper_ids") or []}
    for item in record.get("directed_results") or []:
        if isinstance(item, Mapping):
            for paper_id in item.get("consumed_paper_ids", item.get("attempted_paper_ids") or []):
                output.add(_text(paper_id))
    return {item for item in output if item}


def _directed_task_requirements(task: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize the scientific request fields used to decide read reuse."""

    task = task if isinstance(task, Mapping) else {}
    gaps = task.get("knowledge_gaps") or task.get("knowledge_gap") or task.get("reasons") or task.get("reason") or []
    if isinstance(gaps, str):
        gaps = [gaps]
    gaps = sorted({_text(value) for value in gaps if _text(value)})
    raw_outputs = task.get("required_outputs") or []
    if isinstance(raw_outputs, (str, Mapping)):
        raw_outputs = [raw_outputs]
    outputs: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_outputs, start=1):
        item = dict(raw) if isinstance(raw, Mapping) else {"description": _text(raw)}
        outputs.append({
            "output_id": _text(item.get("output_id") or item.get("id")) or f"O{index:02d}",
            "output_type": _text(item.get("output_type") or item.get("type") or "practical_material"),
            "description": _text(item.get("description") or item.get("purpose") or item.get("required_output")),
        })
    explicit_outputs = bool(outputs)
    output_ids = [row["output_id"] for row in outputs]
    raw_questions = task.get("questions") or []
    if not raw_questions and task.get("question"):
        raw_questions = [task.get("question")]
    if isinstance(raw_questions, (str, Mapping)):
        raw_questions = [raw_questions]
    questions: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_questions, start=1):
        item = dict(raw) if isinstance(raw, Mapping) else {"question": _text(raw)}
        question = _text(item.get("question") or item.get("ask") or item.get("what_to_extract"))
        purpose = _text(item.get("purpose") or item.get("use") or item.get("required_output") or question)
        output_ids = item.get("required_output_ids") or item.get("output_ids") or []
        if isinstance(output_ids, str):
            output_ids = [output_ids]
        output_ids = [_text(value) for value in output_ids if _text(value)]
        if not output_ids:
            if explicit_outputs:
                output_ids = [outputs[min(index - 1, len(outputs) - 1)]["output_id"]]
            else:
                output_id = f"O{index:02d}"
                outputs.append({
                    "output_id": output_id,
                    "output_type": "practical_material",
                    "description": _text(item.get("purpose") or item.get("required_output") or question),
                })
                output_ids.append(output_id)
        questions.append({
            "question_id": _text(item.get("question_id") or item.get("id")) or f"Q{index:02d}",
            "question": question,
            "purpose": purpose,
            "gap_key": _text(item.get("gap_key") or item.get("knowledge_gap")) or "; ".join(gaps),
            "required_output_ids": sorted(set(output_ids)),
        })
    if not questions:
        question = _text(task.get("knowledge_gap") or task.get("reason") or "Extract the paper's evidence relevant to the coordinated review scope.")
        if not outputs:
            outputs = [{"output_id": "O01", "output_type": "practical_material", "description": "Source-based findings, conditions, methods and limits useful to the review."}]
        questions = [{
            "question_id": "Q01", "question": question,
            "purpose": "Supply a concrete, attributed case for the coordinated review.",
            "gap_key": "; ".join(gaps),
            "required_output_ids": sorted(row["output_id"] for row in outputs),
        }]
    return {
        "questions": questions,
        "required_outputs": outputs,
        "knowledge_gaps": sorted({_text(value) for value in gaps if _text(value)}),
    }


def _directed_task_signature(task: Mapping[str, Any] | None) -> str:
    return _material_content_signature(_directed_task_requirements(task))


def _material_task_signature(material: Mapping[str, Any] | None) -> str:
    if not isinstance(material, Mapping):
        return ""
    for key in ("_progressive_task_signature", "progressive_task_signature", "task_signature"):
        signature = _text(material.get(key))
        if signature:
            return signature
    provenance = material.get("output_provenance") if isinstance(material.get("output_provenance"), Mapping) else {}
    return _text(provenance.get("task_signature"))


def _directed_material_compatible(task: Mapping[str, Any], material: Mapping[str, Any] | None) -> bool:
    """Return true only for an explicitly task-bound prior reading.

    Older paper-only artifacts remain useful context, but they are not proof
    that the current question or required output was answered.
    """

    expected = _directed_task_signature(task)
    actual = _material_task_signature(material)
    # A cached task-bound response with no current answer is incomplete.  It
    # remains useful history, but must not suppress a later explicit retry.
    return bool(expected and actual and expected == actual and _directed_material_status(task, material) == "fulfilled")


def _directed_material_status(task: Mapping[str, Any], material: Mapping[str, Any] | None) -> str:
    """Classify only the current answer, never a question row or retained history."""
    from .directed_reading import practical_result_status

    if not isinstance(material, Mapping):
        return "unmet"
    current = dict(material)
    if "current_question_material" in material:
        rows = _material_current_question_rows(material)
        current["question_material"] = rows
        current["content"] = {
            **(dict(material["content"]) if isinstance(material.get("content"), Mapping) else {}),
            "question_material": rows,
        }
    return practical_result_status(current, _directed_task_requirements(task)["questions"])


def _material_question_rows(material: Mapping[str, Any] | None) -> list[Any]:
    if not isinstance(material, Mapping):
        return []
    rows = material.get("question_material")
    if not isinstance(rows, list):
        content = material.get("content") if isinstance(material.get("content"), Mapping) else {}
        rows = content.get("question_material")
    return list(rows) if isinstance(rows, list) else []


def _material_current_question_rows(material: Mapping[str, Any] | None) -> list[Any]:
    """Return rows for the current request, excluding retained history."""

    if not isinstance(material, Mapping):
        return []
    rows = material.get("current_question_material")
    if isinstance(rows, list):
        return list(rows)
    return _material_question_rows(material)


def _unique_question_rows(rows: Sequence[Any]) -> list[Any]:
    output: list[Any] = []
    seen: set[str] = set()
    for row in rows:
        key = json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_default)
        if key not in seen:
            seen.add(key)
            output.append(row)
    return output


def _decorate_directed_material(
    material: Mapping[str, Any],
    *,
    task: Mapping[str, Any],
    prior_material: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind a returned reading to its request and retain legacy question output."""

    output = dict(material)
    current_identity = {**(dict(material["paper_identity"]) if isinstance(material.get("paper_identity"), Mapping) else {}),
                        **{key: material[key] for key in ("paper_id", "canonical_paper_id", "doi") if material.get(key)}}
    if prior_material and _saved_card_identity_conflict(current_identity, prior_material) is not None:
        prior_material = None  # Do not splice another paper's answer into history.
    if prior_material and _prior_read_source_hash(material) and _prior_read_source_hash(material) != _prior_read_source_hash(prior_material):
        # Earlier artifacts stay intact on disk. Do not rebind superseded
        # findings or reference numbers to corrected current source content.
        output["prior_reading_compatibility"] = "source_changed_or_unverified"
        prior_material = None
    output["_progressive_task_signature"] = _directed_task_signature(task)
    output["_progressive_task_requirements"] = _directed_task_requirements(task)
    if prior_material and not _directed_material_compatible(task, prior_material):
        prior_questions = _material_question_rows(prior_material)
        current_questions = _material_current_question_rows(material)
        if prior_questions:
            # Keep the complete reading history in the field consumed by
            # material compaction and chapter payload construction.  The
            # current rows remain separate so fulfillment of the new request
            # never succeeds merely because an older answer was retained.
            output["prior_question_material"] = _unique_question_rows(prior_questions)
            output["current_question_material"] = current_questions
            output["question_material"] = _unique_question_rows([
                *prior_questions, *_material_question_rows(material), *current_questions,
            ])
    elif "current_question_material" not in output:
        current_questions = _material_question_rows(material)
        if current_questions:
            output["current_question_material"] = current_questions
    return output


def _blocked_directed_ids(record: Mapping[str, Any]) -> set[str]:
    output: set[str] = set()
    for item in record.get("directed_results") or []:
        if isinstance(item, Mapping):
            output.update(_text(value) for value in item.get("blocked_paper_ids") or [])
    return {item for item in output if item}


class ProgressiveReviewPlanner:
    def __init__(
        self,
        config: ProgressivePlannerConfig,
        *,
        planner: Callable[[str, Mapping[str, Any]], Any],
        supplement_runner: Callable[..., Mapping[str, Any]] | None = None,
        directed_reader: Callable[..., Mapping[str, Any]] | None = None,
        prior_readings: Sequence[Mapping[str, Any]] = (),
        tool_materials_by_chapter: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        retrieval_loop_runner: Callable[..., Mapping[str, Any]] | None = None,
    ):
        if not _text(config.topic_id):
            raise ProgressivePlanError("topic_id_required")
        if config.shared_deep_read_budget < 0:
            raise ProgressivePlanError("shared_deep_read_budget_must_be_nonnegative")
        self.config = config
        self.planner = planner
        self.supplement_runner = supplement_runner
        self.directed_reader = directed_reader
        self.prior_readings = [dict(row) for row in prior_readings if isinstance(row, Mapping)]
        # Material gathered by the shared retrieval queue (work orders 03/04) is
        # handed to the chapter expansion of the chapter it was gathered for.
        self.tool_materials_by_chapter = {
            str(key): [dict(item) for item in value if isinstance(item, Mapping)]
            for key, value in (tool_materials_by_chapter or {}).items()
        }
        # The CLI supplies the production adaptive queue.  Keeping this
        # injectable preserves the offline planner fixtures and makes the
        # queue boundary observable without contacting a provider.
        self.retrieval_loop_runner = retrieval_loop_runner
        self._read_materials: dict[str, dict[str, Any]] = {}

    def _cache_contract(self, stage: str, payload: Any) -> str:
        """Fingerprint effective input and live prompt/settings, not storage locations.

        The explicit metadata allowlist deliberately retains scientific dates,
        years, durations and conditions. Unknown fields remain conservative.
        """
        metadata = {"card_path", "output_dir", "output_path", "pool_path", "plan_path",
                    "created_at", "updated_at", "generated_at", "retrieved_at",
                    "cache_path", "raw_response_path", "result_path", "reading_path", "snapshot_path",
                    "fetched_at", "saved_at", "telemetry", "reused_from"}
        # Material envelopes retain operational paths/timestamps. Protect their
        # substantive children, rather than treating an entire envelope as science.
        scientific_fields = {"study_summary_A", "review_planning_B", "B_review_planning",
                             "conditions", "finding", "findings", "content", "question_material",
                             "paragraph_briefs", "required_outputs"}

        def project(value: Any, scientific: bool = False) -> Any:
            if isinstance(value, Mapping):
                return {str(key): project(item, scientific or key in scientific_fields)
                        for key, item in sorted(value.items(), key=lambda row: str(row[0]))
                        if scientific or key not in metadata}
            if isinstance(value, (list, tuple)):
                return [project(item, scientific) for item in value]
            return value

        stage = {"harmonized_scope": "harmonize_scope"}.get(stage, stage)
        payload = project(payload)
        model = getattr(self.planner, "chapter_model", self.config.chapter_model) if stage in {"chapter_details", "case_groups"} else getattr(self.planner, "model", self.config.planner_model)
        output_tokens = getattr(self.planner, "output_tokens", self.config.planner_output_tokens)
        thinking_budget = getattr(self.planner, "thinking_budget", self.config.thinking_budget)
        if stage in {"source_routing", "chapter_details", "case_groups"}:
            thinking_budget = 2048
        if stage in {"chapter_details", "affected_chapter_revision"}:
            output_tokens = min(output_tokens, 16000)
        if stage == "whole_plan_improvement":
            output_tokens = min(output_tokens, 5000)
        if stage in {"whole_plan_improvement", "affected_chapter_revision"}:
            thinking_budget = min(thinking_budget, 1024)
        contract = {"version": 1, "stage": stage,
                    # Tool orchestration has no planner prompt of its own; its
                    # complete outer arguments are the compatibility boundary.
                    "messages": payload if stage.endswith("_tools") else _messages_for(stage, payload if isinstance(payload, Mapping) else {"inputs": payload}),
                    "model": model, "output_tokens": output_tokens, "thinking_budget": thinking_budget}
        return hashlib.sha256(json.dumps(contract, ensure_ascii=False, sort_keys=True, default=_json_default).encode("utf-8")).hexdigest()

    def _model_stage(self, name: str, payload: Mapping[str, Any], *, resume: bool, state: dict[str, Any]) -> Any:
        # Use one frozen payload for the actual call and its persisted provenance.
        payload = json.loads(json.dumps(payload, default=_json_default))
        stage = {"harmonized_scope": "harmonize_scope"}.get(name, name)
        return self._stage(name, lambda: _call_record(self.planner, stage, payload),
                           resume=resume, state=state, cache_inputs=payload)

    def _stage(self, name: str, fn: Callable[[], Any], *, resume: bool, state: dict[str, Any], cache_inputs: Any = None) -> Any:
        path = self.config.output_dir / "stages" / (name + ".json")
        # Snapshot before invoking a callable which may mutate its inputs.
        frozen_inputs = json.loads(json.dumps(cache_inputs, default=_json_default)) if cache_inputs is not None else None
        contract = self._cache_contract(name, frozen_inputs) if frozen_inputs is not None else None
        if resume and path.is_file() and contract is not None:
            if (state.get("stage_cache_contracts") or {}).get(name) == contract:
                return _read_json(path)
        state.update({"status": "in_progress", "current_stage": name})
        _atomic_json(self.config.output_dir / "RUN_STATE.json", state)
        result = fn()
        _atomic_json(path, result)
        if cache_inputs is not None:
            stage_inputs = dict(state.get("stage_inputs") or {})
            stage_inputs[name] = frozen_inputs
            state["stage_inputs"] = stage_inputs
        contracts = dict(state.get("stage_cache_contracts") or {})
        if contract is None:
            contracts.pop(name, None)
        else:
            contracts[name] = contract
        state["stage_cache_contracts"] = contracts
        completed = list(state.get("completed_stages") or [])
        if name not in completed:
            completed.append(name)
        state.update({"status": "in_progress", "current_stage": "", "completed_stages": completed})
        _atomic_json(self.config.output_dir / "RUN_STATE.json", state)
        return result

    def revise_from_arrangement_issues(
        self,
        *,
        chapter: Mapping[str, Any],
        chapter_plan: Mapping[str, Any],
        source_materials: Sequence[Mapping[str, Any]],
        arrangement: Mapping[str, Any],
        research_question: str = "",
        candidate_navigation: Mapping[str, Any] | None = None,
        candidate_materials: Sequence[Mapping[str, Any]] = (),
        tool_materials: Sequence[Mapping[str, Any]] = (),
        feedback_materials: Sequence[Mapping[str, Any]] = (),
        pool_rows: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Consume arrangement issues through the existing chapter-owner role.

        Empty feedback reuses the current plan.  Non-empty feedback invokes
        ``affected_chapter_revision`` and returns the rebuilt plan together
        with the exact owner payload for the next arrangement/writing step.
        """

        owner_source_materials, owner_material_resolution = _resolve_owner_source_materials(
            source_materials=source_materials,
            chapter_plan=chapter_plan,
            chapter_feedback=arrangement.get("issues") or (),
            candidate_navigation=candidate_navigation,
            candidate_materials=candidate_materials,
            pool_rows=pool_rows,
            deep_material_by_paper=self._read_materials,
            tool_materials=tool_materials, feedback_materials=feedback_materials,
        )
        payload = build_arrangement_issue_revision_payload(
            chapter=chapter,
            chapter_plan=chapter_plan,
            source_materials=owner_source_materials,
            arrangement=arrangement,
            topic_id=self.config.topic_id,
            research_question=research_question,
            candidate_navigation=candidate_navigation,
            candidate_materials=candidate_materials,
            tool_materials=tool_materials,
            feedback_materials=feedback_materials,
        )
        payload["owner_material_resolution"] = owner_material_resolution
        issues = payload["arrangement_issues"]
        if not issues:
            return {
                "status": "reused",
                "issues": [],
                "owner_payload": payload,
                "updated_plan": dict(chapter_plan),
                "unit_id_remap": {},
                "rebuild_arrangement_input": {"chapter": dict(chapter), "chapter_plan": dict(chapter_plan),
                                               "source_materials": [dict(item) for item in owner_source_materials if isinstance(item, Mapping)]},
            }
        record = _call_record(self.planner, "affected_chapter_revision", payload)
        response = _stage_response(record)
        owner_input_plan = payload.get("chapter_plan") if isinstance(payload.get("chapter_plan"), Mapping) else chapter_plan
        adopted_materials, adoption_resolution = _close_owner_response_materials(payload, response)
        status, updated_plan, unit_id_remap, structural_errors = _classify_owner_response(
            owner_input_plan, response, adopted_materials,
        )
        if status in {"updated", "no_change"}:
            owner_source_materials = adopted_materials
        return {
            "adoption_material_resolution": adoption_resolution,
            "status": status,
            "issues": issues,
            "owner_payload": payload,
            "owner_response": response,
            "updated_plan": updated_plan,
            "unit_id_remap": unit_id_remap,
            "structural_errors": structural_errors,
            "rebuild_arrangement_input": {
                "chapter": dict(chapter),
                "chapter_plan": updated_plan if updated_plan is not None else dict(chapter_plan),
                "source_materials": [dict(item) for item in owner_source_materials if isinstance(item, Mapping)],
            },
        }

    def _propose_chapters(self, *, topic: str, outline: Mapping[str, Any],
                          routing: Mapping[str, Any], resume: bool) -> dict[str, Any]:
        """Plan chapter content separately; the local route ledger keeps the bibliography."""
        chapters = _outline_chapter_rows(outline.get("shared_outline"))
        root = self.config.output_dir / "stages" / "chapter_proposals"
        root.mkdir(parents=True, exist_ok=True)

        def propose(chapter: Mapping[str, Any]) -> dict[str, Any]:
            chapter_id = _text(chapter.get("chapter_id") or chapter.get("id"))
            path = root / (_safe_id(chapter_id) + ".json")
            routes = [row for row in routing["source_routes"] if chapter_id in (row.get("chapter_ids") or [])]
            material = [{key: row[key] for key in (
                "question", "usable_content", "still_missing", "conditions", "limits", "source_handles"
            ) if key in row} for row in self.tool_materials_by_chapter.get(chapter_id, [])]
            payload = {
                "call_id": "chapter-proposal-" + chapter_id,
                "topic_id": self.config.topic_id, "research_question": topic,
                "chapter": dict(chapter), "shared_level1_outline": outline,
                "source_routing": routes, "chapter_tool_materials": material,
                "candidate_pool_row_count": routing.get("pool_sources"),
            }
            contract = self._cache_contract("chapter_proposals", payload)
            if resume and path.is_file():
                cached = _read_json(path)
                if isinstance(cached, Mapping) and cached.get("cache_contract") == contract:
                    return cached
            record = _call_record(self.planner, "chapter_proposals", payload)
            response = _normalize_proposal_response(_stage_response(record))
            rows = [row for row in response.get("chapter_proposals", [])
                    if isinstance(row, Mapping) and _text(row.get("chapter_id") or row.get("id")) == chapter_id]
            if not rows:
                raise ProgressivePlanError("chapter_proposal_missing:" + chapter_id)
            result = {"response": {"chapter_proposals": rows}, "telemetry": record.get("telemetry") or {}}
            result["cache_contract"] = contract
            _atomic_json(path, result)
            return result

        with ThreadPoolExecutor(max_workers=max(1, self.config.chapter_workers)) as executor:
            records = list(executor.map(propose, chapters))
        return {"response": {"chapter_proposals": [row for record in records
                    for row in _stage_response(record).get("chapter_proposals", [])]},
                "telemetry": {"chapter_calls": [record.get("telemetry") or {} for record in records]}}

    def _route_sources(
        self,
        pool_rows: Sequence[Mapping[str, Any]],
        *,
        shared_outline: Any,
        resume: bool,
        state: dict[str, Any],
        cache_namespace: str = "source_routing",
    ) -> dict[str, Any]:
        """Semantically route every full-pool source in bounded cached batches."""
        route_root = self.config.output_dir / "stages" / cache_namespace
        route_root.mkdir(parents=True, exist_ok=True)
        chapters = _outline_chapter_rows(shared_outline)
        chapter_ids = [_text(row.get("chapter_id") or row.get("id")) for row in chapters]
        chapter_ids = [item for item in chapter_ids if item]
        batches = [list(pool_rows[index:index + SOURCE_ROUTING_BATCH_SIZE]) for index in range(0, len(pool_rows), SOURCE_ROUTING_BATCH_SIZE)]
        state.update({"status": "in_progress", "current_stage": "source_routing", "source_routing_batches": len(batches)})
        _atomic_json(self.config.output_dir / "RUN_STATE.json", state)
        stop_event = threading.Event()
        transport_failures: list[QwenTransportError] = []

        def route_batch(index: int, rows: Sequence[Mapping[str, Any]]) -> tuple[int, dict[str, Any]]:
            if stop_event.is_set():
                raise ProgressivePlanError("source_routing_cancelled")
            batch_id = f"batch_{index + 1:03d}"
            cache_path = route_root / f"{batch_id}.json"
            candidate_batch = []
            for row in rows:
                note = row.get("_b_summary", {}).get("root_review_note")
                if isinstance(note, Mapping):
                    note = {key: value for key, value in note.items() if key != "paper_id"}
                candidate_batch.append({
                    "source_handle": _text(row.get("_source_handle")),
                    "paper_title": _text((row.get("_b_summary") or {}).get("title")),
                    "doi": _text((row.get("_b_summary") or {}).get("doi")),
                    "year": _text((row.get("_b_summary") or {}).get("year")),
                    "material_scope": _text((row.get("_b_summary") or {}).get("material_scope")),
                    "material_depth": _text((row.get("_b_summary") or {}).get("declared_content_depth")),
                    "B_review_planning": {
                        key: value for key, value in (row.get("_b_summary") or {}).items()
                        if key != "root_review_note"
                    },
                    "curator_note": note or {},
                    "supplement_material": row.get("supplement_gap_material") or {},
                    "other_supplement_materials": row.get("supplement_gap_materials") or [],
                })
            expected_handles = [row["source_handle"] for row in candidate_batch]
            valid_chapters = set(chapter_ids)

            def normalize_routes(candidates: Sequence[Mapping[str, Any]], raw_routes: Any) -> list[dict[str, Any]]:
                by_handle: dict[str, Mapping[str, Any]] = {}
                if isinstance(raw_routes, list):
                    for route in raw_routes:
                        if isinstance(route, Mapping):
                            handle = _text(route.get("source_handle") or route.get("source_id"))
                            if handle and handle not in by_handle:
                                by_handle[handle] = route
                output_routes = []
                for candidate in candidates:
                    handle = candidate["source_handle"]
                    route = by_handle.get(handle, {})
                    raw_chapters = route.get("chapter_ids") or []
                    if isinstance(raw_chapters, str):
                        raw_chapters = [raw_chapters]
                    assigned = list(dict.fromkeys(
                        _text(value.get("chapter_id") or value.get("id")) if isinstance(value, Mapping) else _text(value)
                        for value in raw_chapters
                    ))
                    assigned = [chapter_id for chapter_id in assigned if chapter_id and (not valid_chapters or chapter_id in valid_chapters)]
                    useful = _text(route.get("specific_usable_material") or route.get("usable_material") or route.get("substantive_use") or route.get("use"))
                    reason = _text(route.get("reason") or route.get("non_use_reason"))
                    if route and not assigned:
                        reason = reason or useful or "当前共享提纲下无明确章节用途。"
                    output_routes.append({
                        "source_handle": handle,
                        "paper_title": candidate["paper_title"],
                        "chapter_ids": assigned,
                        "specific_usable_material": useful,
                        "route_status": "assigned" if assigned else ("not_relevant" if route else "route_result_missing"),
                        "reason": reason,
                    })
                return output_routes

            def failed_routes(candidates: Sequence[Mapping[str, Any]], reason: str) -> list[dict[str, Any]]:
                return [{
                    "source_handle": row["source_handle"],
                    "paper_title": row["paper_title"],
                    "chapter_ids": [],
                    "specific_usable_material": "",
                    "route_status": "route_call_failed",
                    "reason": reason,
                } for row in candidates]

            def route_payload(candidates: Sequence[Mapping[str, Any]], *, repair_attempt: int = 0) -> dict[str, Any]:
                suffix = f"-repair-{repair_attempt:03d}" if repair_attempt else ""
                payload = {
                    "call_id": f"{cache_namespace.replace('_', '-')}-{batch_id}-of-{len(batches):03d}{suffix}",
                    "topic_id": self.config.topic_id,
                    "research_question": _text(state.get("topic")),
                    "shared_outline": _compact_routing_outline(shared_outline),
                    "complete_pool_size": len(pool_rows),
                    "batch_index": index + 1,
                    "batch_count": len(batches),
                    "candidate_batch": [dict(row) for row in candidates],
                    "chapter_ids": chapter_ids,
                    "output_contract": {
                        "array_key": "source_routes",
                        "one_row_per_source_handle": True,
                        "route_every_input": True,
                        "target_chinese_characters_per_useful_source": "80-150",
                        "do_not_invent_sources": True,
                    },
                }
                if repair_attempt:
                    payload["repair_for_batch"] = batch_id
                    payload["repair_attempt"] = repair_attempt
                    payload["route_only_supplied_handles"] = True
                return payload

            contract = self._cache_contract("source_routing", route_payload(candidate_batch))

            def invoke(candidates: Sequence[Mapping[str, Any]], *, repair_attempt: int = 0) -> tuple[list[dict[str, Any]], dict[str, Any]]:
                if stop_event.is_set():
                    raise ProgressivePlanError("source_routing_cancelled")
                payload = route_payload(candidates, repair_attempt=repair_attempt)
                try:
                    record = _call_record(self.planner, "source_routing", payload)
                    response = _stage_response(record)
                    raw_routes = response.get("source_routes") or response.get("routes") or []
                    return normalize_routes(candidates, raw_routes), dict(record.get("telemetry") or {})
                except QwenTransportError as exc:
                    stop_event.set()
                    transport_failures.append(exc)
                    raise
                except Exception as exc:
                    return failed_routes(candidates, type(exc).__name__), {"error": type(exc).__name__}

            def pending_from_cache(cached: Mapping[str, Any]) -> tuple[list[dict[str, Any]], set[str]]:
                attempted = {
                    _text(handle) for handle in (cached.get("repair_attempted_handles") or []) if _text(handle)
                }
                by_handle = {
                    _text(route.get("source_handle")): route
                    for route in (cached.get("source_routes") or [])
                    if isinstance(route, Mapping) and _text(route.get("source_handle"))
                }
                pending = []
                for candidate in candidate_batch:
                    handle = candidate["source_handle"]
                    route = by_handle.get(handle) or {}
                    status = _text(route.get("route_status"))
                    if status in {"assigned", "not_relevant"} or handle in attempted:
                        continue
                    pending.append(candidate)
                return pending, attempted

            def repair_cached(cached: Mapping[str, Any], pending: Sequence[Mapping[str, Any]], attempted: set[str]) -> dict[str, Any]:
                repaired, telemetry = invoke(pending, repair_attempt=1)
                by_handle = {
                    _text(route.get("source_handle")): dict(route)
                    for route in (cached.get("source_routes") or [])
                    if isinstance(route, Mapping) and _text(route.get("source_handle"))
                }
                for route in repaired:
                    by_handle[route["source_handle"]] = route
                merged_routes = [by_handle[handle] for handle in expected_handles if handle in by_handle]
                saved = dict(cached)
                saved["source_handles"] = expected_handles
                saved["source_routes"] = merged_routes
                saved["repair_attempted_handles"] = sorted(attempted | {row["source_handle"] for row in pending})
                saved["repair_telemetry"] = telemetry
                _atomic_json(cache_path, saved)
                return saved

            if resume and cache_path.is_file():
                try:
                    cached = _read_json(cache_path)
                    if isinstance(cached, Mapping) and cached.get("cache_contract") == contract:
                        pending, attempted = pending_from_cache(cached)
                        if not pending:
                            return index, dict(cached)
                        return index, repair_cached(cached, pending, attempted)
                except ProgressivePlanError:
                    pass

            output_routes, telemetry = invoke(candidate_batch)
            saved = {
                "cache_contract": contract,
                "batch_id": batch_id,
                "batch_index": index + 1,
                "batch_count": len(batches),
                "source_handles": expected_handles,
                "source_routes": output_routes,
                "telemetry": telemetry,
            }
            pending, attempted = pending_from_cache(saved)
            if pending:
                saved = repair_cached(saved, pending, attempted)
            _atomic_json(cache_path, saved)
            return index, saved

        indexed: dict[int, dict[str, Any]] = {}
        executor = ThreadPoolExecutor(max_workers=min(SOURCE_ROUTING_WORKERS, max(1, self.config.chapter_workers)))
        futures = []
        try:
            futures = [executor.submit(route_batch, index, batch) for index, batch in enumerate(batches)]
            for future in as_completed(futures):
                try:
                    index, record = future.result()
                except Exception:
                    stop_event.set()
                    for pending in futures:
                        pending.cancel()
                    if transport_failures:
                        raise transport_failures[0]
                    raise
                indexed[index] = record
        finally:
            if stop_event.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
            else:
                executor.shutdown(wait=True)
        ordered_batches = [indexed[index] for index in range(len(batches))]
        routes = [route for batch in ordered_batches for route in batch.get("source_routes") or []]
        state["completed_stages"] = list(dict.fromkeys([*(state.get("completed_stages") or []), "source_routing"]))
        state.update({"status": "in_progress", "current_stage": ""})
        _atomic_json(self.config.output_dir / "RUN_STATE.json", state)
        return {
            "pool_sources": len(pool_rows),
            "batch_size": SOURCE_ROUTING_BATCH_SIZE,
            "batch_count": len(batches),
            "routed_sources": len(routes),
            "assigned_sources": sum(bool(row.get("chapter_ids")) for row in routes),
            "batches": ordered_batches,
            "source_routes": routes,
        }

    def _route_late_sources(
        self,
        routing: Mapping[str, Any],
        pool_rows: Sequence[Mapping[str, Any]],
        *,
        detail_records: Sequence[Mapping[str, Any]],
        tool_results: Sequence[Mapping[str, Any]],
        shared_outline: Any,
        resume: bool,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Give only newly admitted sources a final, chapter-scoped opportunity.

        Ownership is context, not mandatory case selection. Identity-only rows
        remain visible but cannot supply invented research material. Unknown
        ownership reuses the existing router in an independent incremental cache.
        """
        existing = {_text(row.get("source_handle")) for row in routing.get("source_routes") or []
                    if isinstance(row, Mapping)}
        # Owner-admitted tool-only studies need not have their own pool card.
        # Accept only their resolved packet identity, never an unbound handle.
        candidates = list(pool_rows)
        candidate_handles = {_text(row.get("_source_handle")) for row in candidates}
        pool_identities = {_text(row.get("_source_handle")): {
            "paper_id": _text(row.get("_paper_id") or _canonical_paper_id(row)),
            "doi": _text((row.get("_b_summary") or {}).get("doi") or row.get("doi")),
        } for row in pool_rows}
        conflicts: set[str] = set()
        packet_identities: dict[str, Mapping[str, Any]] = {}
        for record in detail_records:
            identity_map = record.get("source_identity_map") or {}
            for material in record.get("source_materials") or []:
                if not isinstance(material, Mapping):
                    continue
                handle = _text(material.get("source_handle"))
                identity = identity_map.get(handle) or {}
                previous = packet_identities.get(handle)
                if (material.get("material_identity_conflict")
                        or _owner_identity_conflict(material, identity)
                        or _owner_identity_conflict(material, pool_identities.get(handle) or {})
                        or (previous and _owner_identity_conflict(previous, material))):
                    conflicts.add(handle)
                packet_identities.setdefault(handle, material)
                if (not handle or handle in candidate_handles or not identity
                        or not _text(identity.get("paper_id") or identity.get("doi"))):
                    continue
                candidates.append({"_source_handle": handle,
                    "_paper_id": _text(identity.get("paper_id")),
                    "_b_summary": {key: identity.get(key) for key in ("title", "doi", "year")},
                    "supplement_gap_material": dict(material)})
                candidate_handles.add(handle)
        late = [row for row in candidates if _text(row.get("_source_handle"))
                and _text(row.get("_source_handle")) not in existing]
        if not late:
            return dict(routing)
        chapters = [dict(record.get("chapter") or {}) for record in detail_records]
        valid_chapters = {_text(row.get("chapter_id") or row.get("id")) for row in chapters}
        valid_chapters.discard("")
        for result in tool_results:
            chapters = _attach_chapter_candidate_sources(chapters, pool_rows, result)
        chapters = _attach_chapter_candidate_sources(chapters, pool_rows, {
            "tool_materials_by_chapter": self.tool_materials_by_chapter,
        })
        owners: dict[str, list[str]] = {}
        paper_to_handle = {_text(row.get("_paper_id")): _text(row.get("_source_handle")) for row in pool_rows}
        for chapter in chapters:
            chapter_id = _text(chapter.get("chapter_id") or chapter.get("id"))
            handles, papers = _source_keys_in_value(chapter)
            handles.update(paper_to_handle[paper] for paper in papers if paper in paper_to_handle)
            for handle in handles:
                if chapter_id in valid_chapters:
                    owners.setdefault(handle, []).append(chapter_id)
        for record in detail_records:
            chapter_id = _text((record.get("chapter") or {}).get("chapter_id"))
            # Candidate navigation can contain the entire pool. Only adopted
            # source material and selected plan handles establish ownership.
            handles, papers = _source_keys_in_value(record.get("chapter_plan") or {})
            handles.update(_text(row.get("source_handle")) for row in record.get("source_materials") or []
                           if isinstance(row, Mapping))
            handles.update(paper_to_handle[paper] for paper in papers if paper in paper_to_handle)
            for handle in handles:
                if handle and chapter_id in valid_chapters:
                    owners.setdefault(handle, []).append(chapter_id)
        material_by_handle = {row["source_handle"]: row for row in _case_selection_material_rows(
            [_text(row.get("_source_handle")) for row in late], detail_records, pool_rows, self._read_materials)}
        added: dict[str, dict[str, Any]] = {}
        unknown: list[dict[str, Any]] = []
        for candidate in late:
            handle = _text(candidate.get("_source_handle"))
            material = material_by_handle.get(handle) or {}
            chapter_ids = list(dict.fromkeys(owners.get(handle) or []))
            base = {
                "source_handle": handle,
                "paper_title": _text(material.get("title") or (candidate.get("_b_summary") or {}).get("title")),
                "chapter_ids": [],
                "specific_usable_material": "",
                "interpretation_limits": (candidate.get("_b_summary") or {}).get("scope_interpretation_cautions") or [],
            }
            if handle in conflicts:
                added[handle] = {**base, "route_status": "late_identity_conflict",
                                 "reason": "Conflicting late-source identities require resolution.",
                                 "known_chapter_ids": chapter_ids}
            elif not _owner_material_has_content(material):
                added[handle] = {**base, "route_status": "late_material_unavailable",
                                 "reason": "Late identity retained; substantive study material is unavailable.",
                                 "known_chapter_ids": chapter_ids}
            elif chapter_ids:
                content = {key: value for key, value in material.items()
                           if key in {"study_summary_A", "review_planning_B", "supplement_gap_material",
                                      "supplement_gap_materials", "supplement_material", "supplement_materials",
                                      "tool_supplement_materials", "tool_materials", "usable_content", "material",
                                      "local_passages", "deep_read_material"}}
                added[handle] = {**base, "chapter_ids": chapter_ids, "route_status": "assigned",
                                 "specific_usable_material": json.dumps(content, ensure_ascii=False, default=_json_default)[:4000],
                                 "reason": "Known chapter/use context for a late source."}
            else:
                # Route the exact material that case selection will receive,
                # including review-derived content without requiring own A/B.
                unknown.append({**dict(candidate), "supplement_gap_material": material})
        if unknown:
            increment = self._route_sources(unknown, shared_outline=shared_outline,
                resume=resume, state=state, cache_namespace="late_source_routing")
            for route in increment["source_routes"]:
                handle = _text(route.get("source_handle"))
                added[handle] = {**dict(route), "interpretation_limits":
                    next((row.get("_b_summary") or {}).get("scope_interpretation_cautions") or []
                         for row in unknown if _text(row.get("_source_handle")) == handle)}
        result = dict(routing)
        result["late_source_routes"] = [added[_text(row.get("_source_handle"))] for row in late]
        result["source_routes"] = [*(routing.get("source_routes") or []), *result["late_source_routes"]]
        result["pool_sources"] = len(pool_rows)
        result["late_packet_sources"] = len(candidates) - len(pool_rows)
        result["routed_sources"] = len(result["source_routes"])
        result["assigned_sources"] = sum(bool(row.get("chapter_ids")) for row in result["source_routes"])
        _atomic_json(self.config.output_dir / "stages" / "source_routing_summary.json", result)
        return result

    def _tool_cycle(
        self,
        *,
        phase: str,
        supplement_requests: Sequence[Mapping[str, Any]],
        directed_requests: Sequence[Mapping[str, Any]],
        pool_rows: Sequence[Mapping[str, Any]],
        plan: Mapping[str, Any],
        prior_directed: Mapping[str, Any] | None,
        prior_tool_results: Mapping[str, Any] | None,
        source_handle_map: Mapping[str, str],
        resume: bool,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        name = phase.lower() + "_tools"
        cycle_inputs = {
            "phase": phase, "topic_id": self.config.topic_id,
            "supplement_requests": _normalize_gaps(supplement_requests),
            "directed_requests": _merge_directed_tasks(directed_requests),
            "pool_rows": list(pool_rows), "plan": dict(plan),
            "prior_directed": dict(prior_directed or {}),
            "prior_tool_results": dict(prior_tool_results or {}),
            "source_handle_map": dict(source_handle_map),
            "prior_readings": self.prior_readings,
            "directed_source_materials": {str(row.get("_paper_id") or _canonical_paper_id(row)): _directed_source_state(row)
                                          for row in pool_rows if isinstance(row, Mapping)
                                          and _text(row.get("_paper_id") or _canonical_paper_id(row)) in
                                          {_text(task.get("paper_id")) for task in directed_requests}},
            "shared_deep_read_budget": self.config.shared_deep_read_budget,
            "reader_model": self.config.reader_model,
            "thinking_budget": self.config.thinking_budget,
            "adaptive_queue": self.retrieval_loop_runner is not None,
        }

        if self.retrieval_loop_runner is not None:
            def run_adaptive_cycle() -> dict[str, Any]:
                result = self.retrieval_loop_runner(
                    phase=phase,
                    supplement_requests=_normalize_gaps(supplement_requests),
                    directed_requests=_merge_directed_tasks(directed_requests),
                    pool_rows=pool_rows,
                    plan=plan,
                    prior_tool_results=dict(prior_tool_results or {}),
                    prior_directed=dict(prior_directed or {}),
                    source_handle_map=dict(source_handle_map),
                    resume=resume,
                    output_dir=self.config.output_dir / phase.lower(),
                )
                if not isinstance(result, Mapping):
                    raise ProgressivePlanError("retrieval_loop_runner_return_not_object")
                return dict(result)

            result = self._stage(
                # The collector owns per-need/material compatibility and
                # explicit failure retries. Re-enter it even when the outer
                # arguments match; its durable caches reuse fulfilled work.
                name, run_adaptive_cycle, resume=False, state=state,
                cache_inputs=cycle_inputs,
            )
            for chapter_id, rows in (result.get("tool_materials_by_chapter") or {}).items():
                existing = self.tool_materials_by_chapter.setdefault(str(chapter_id), [])
                for row in rows:
                    if isinstance(row, Mapping):
                        # Each collector row is the cumulative current view
                        # of this exact need. Its prior attempts remain in the
                        # journal; do not keep an obsolete gap/failure active
                        # beside the recovered answer in chapter messages.
                        need_id = _text(row.get("need_id"))
                        if need_id:
                            existing[:] = [item for item in existing if _text(item.get("need_id")) != need_id]
                        seen = {json.dumps(item, ensure_ascii=False, sort_keys=True, default=_json_default) for item in existing}
                        encoded = json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_default)
                        if encoded not in seen:
                            existing.append(dict(row))
                            seen.add(encoded)
            # Keep the adaptive queue's paid readings in the same local store
            # the legacy branch uses, so later phases and packet construction
            # see them without a second retrieval.
            for group in result.get("directed_results") or []:
                if isinstance(group, Mapping):
                    for material in group.get("materials") or []:
                        if isinstance(material, Mapping) and _text(material.get("paper_id")):
                            self._read_materials[_text(material.get("paper_id"))] = dict(material)
            return result

        def run_cycle() -> dict[str, Any]:
            gaps = _normalize_gaps(supplement_requests)
            merged = _merge_directed_tasks(directed_requests)
            pool_by_id = {str(row.get("_paper_id")): dict(row) for row in pool_rows}
            already_read = _all_directed_ids(prior_directed or {})
            blocked_no_reacquire = _blocked_directed_ids(prior_directed or {})
            already_read.update(_text(row.get("paper_id")) for row in self.prior_readings if _text(row.get("paper_id")))
            prior_material_by_id: dict[str, dict[str, Any]] = {}
            for row in self.prior_readings:
                paper_id = _text(row.get("paper_id"))
                if paper_id:
                    prior_material_by_id[paper_id] = dict(row)
                    self._read_materials.setdefault(paper_id, dict(row))
            if prior_directed:
                for result in prior_directed.get("directed_results") or []:
                    if isinstance(result, Mapping):
                        for material in result.get("materials") or []:
                            if isinstance(material, Mapping) and _text(material.get("paper_id")):
                                paper_id = _text(material.get("paper_id"))
                                prior_material_by_id[paper_id] = dict(material)
                                self._read_materials[paper_id] = dict(material)
            available = max(0, int(self.config.shared_deep_read_budget) - len(already_read))
            reused: list[dict[str, Any]] = []
            reused_materials: list[dict[str, Any]] = []
            existing_candidates: list[dict[str, Any]] = []
            new_candidates: list[dict[str, Any]] = []
            for task in merged:
                paper_id = task["paper_id"]
                prior_material = prior_material_by_id.get(paper_id) or self._read_materials.get(paper_id)
                if paper_id in blocked_no_reacquire:
                    reused.append({**task, "status": "review_reported_source_no_reacquire", "material": {}})
                elif prior_material is not None and _directed_material_compatible(task, prior_material) and _prior_read_source_compatible(pool_by_id.get(paper_id, {}), prior_material):
                    reused.append({**task, "status": "reused_prior_deep_read", "material": dict(prior_material)})
                    reused_materials.append(dict(prior_material))
                elif paper_id in already_read:
                    # A changed request for an already-read paper is eligible
                    # for one new task; it does not consume another unique
                    # paper slot. Legacy paper-only material remains context,
                    # never proof that this task was fulfilled.
                    existing_candidates.append({**task, "_prior_material": dict(prior_material or {})})
                else:
                    new_candidates.append(task)
            existing_candidates.sort(key=lambda row: (-float(row.get("priority") or 0), row["paper_id"]))
            new_candidates.sort(key=lambda row: (-float(row.get("priority") or 0), row["paper_id"]))
            selected_new_ids = set(list(dict.fromkeys(row["paper_id"] for row in new_candidates))[:available])
            selected = [*existing_candidates, *[row for row in new_candidates if row["paper_id"] in selected_new_ids]]
            deferred = [{**row, "status": "deferred_global_deep_read_budget"} for row in new_candidates if row["paper_id"] not in selected_new_ids]

            context = {
                "phase": phase,
                "output_dir": self.config.output_dir / phase.lower(),
                "topic_id": self.config.topic_id,
                "plan": dict(plan),
                "pool_rows": pool_rows,
                "pool_by_id": pool_by_id,
                "shared_deep_read_budget": self.config.shared_deep_read_budget,
                "remaining_deep_read_budget": available,
                "prior_readings": self.prior_readings,
                "prior_tool_results": dict(prior_tool_results or {}),
                "source_handle_map": dict(source_handle_map),
            }
            supplement_future = None
            directed_future = None
            with ThreadPoolExecutor(max_workers=2) as executor:
                if gaps and self.supplement_runner is not None:
                    supplement_future = executor.submit(self.supplement_runner, gaps, **context)
                if selected and self.directed_reader is not None:
                    directed_future = executor.submit(self.directed_reader, selected, **context)
                supplement_result = self._collect_tool_result(supplement_future, "supplement_runner_unavailable", gaps)
                directed_result = self._collect_tool_result(directed_future, "directed_reader_unavailable", selected)
            consumed = {_text(item) for item in directed_result.get("consumed_paper_ids") or [] if _text(item)}
            selected_by_signature = {(_text(task.get("paper_id")), _directed_task_signature(task)): task for task in selected}
            selected_by_id: dict[str, list[Mapping[str, Any]]] = {}
            for task in selected:
                selected_by_id.setdefault(_text(task.get("paper_id")), []).append(task)
            decorated_materials: list[dict[str, Any]] = []
            for result in directed_result.get("materials") or []:
                if isinstance(result, Mapping) and _text(result.get("paper_id")):
                    paper_id = _text(result.get("paper_id"))
                    task = selected_by_signature.get((paper_id, _material_task_signature(result)))
                    candidates = selected_by_id.get(paper_id, [])
                    if task is None and len(candidates) == 1:
                        task = candidates[0]
                    if task is not None:
                        result = _decorate_directed_material(
                            result, task=task, prior_material=task.get("_prior_material"),
                        )
                    decorated = dict(result)
                    decorated_materials.append(decorated)
                    self._read_materials[paper_id] = decorated
            decorated_materials = [*reused_materials, *decorated_materials]
            directed_result = {**directed_result, "materials": decorated_materials}
            consumed.update(_text(item.get("paper_id")) for item in reused_materials if _text(item.get("paper_id")))
            directed_result = {
                **directed_result,
                "consumed_paper_ids": sorted(consumed),
                "blocked_paper_ids": sorted(_text(item) for item in directed_result.get("blocked_paper_ids") or [] if _text(item)),
                "reused_prior_tasks": reused,
                "deferred_tasks": deferred,
                "requested_unique_papers": len({row["paper_id"] for row in merged}),
                "selected_unique_papers": len({row["paper_id"] for row in selected}),
                "shared_budget_remaining": max(
                    0,
                    available - len({paper_id for paper_id in consumed if paper_id not in already_read}),
                ),
            }
            return {
                "phase": phase,
                "supplement_requests": gaps,
                "directed_requests": merged,
                "supplement_results": [supplement_result] if supplement_result else [],
                "directed_results": [directed_result] if directed_result else [],
                "consumed_paper_ids": sorted(consumed),
                "budget_deferred_tasks": deferred,
            }

        return self._stage(name, run_cycle, resume=resume, state=state, cache_inputs=cycle_inputs)

    @staticmethod
    def _collect_tool_result(future: Any, absent_reason: str, requested: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        if not requested:
            return {"status": "not_requested", "results": []}
        if future is None:
            return {"status": "unavailable", "reason": absent_reason, "results": []}
        try:
            result = future.result()
        except Exception as exc:
            return {"status": "failed", "error": type(exc).__name__, "results": []}
        if not isinstance(result, Mapping):
            return {"status": "failed", "error": "tool_return_not_object", "results": []}
        return dict(result)

    def run(
        self,
        *,
        resume: bool = False,
        stop_after: str = "",
        editorial_feedback: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if stop_after not in {"", "level1", "level2"}:
            raise ProgressivePlanError("stop_after_must_be_level1_or_level2")
        root = self.config.output_dir.resolve()
        if root.exists() and any(root.iterdir()) and not resume:
            raise ProgressivePlanError("output_directory_exists_use_resume")
        root.mkdir(parents=True, exist_ok=True)
        pool_rows = load_planning_pool(self.config.pool_path)
        plan = load_original_plan(self.config.plan_path)
        topic = _text(plan.get("question_en") or plan.get("question"))
        editorial_feedback = dict(editorial_feedback or {}) if isinstance(editorial_feedback, Mapping) else {}
        source_handle_map = _refresh_source_handles(pool_rows)
        paper_to_handle = {paper_id: handle for handle, paper_id in source_handle_map.items()}
        b_pool = [dict(row["_b_summary"]) for row in pool_rows]
        candidate_by_id = {str(row["_paper_id"]): row for row in pool_rows}
        initial_material_snapshot = _material_theme_snapshot(pool_rows)
        state_path = root / "RUN_STATE.json"
        if resume and state_path.is_file():
            state = dict(_read_json(state_path))
            if str(state.get("topic_id") or "") != self.config.topic_id:
                raise ProgressivePlanError("resume_topic_id_mismatch")
            if bool(state.get("planning_revision_enabled", False)) != bool(self.config.planning_revision_enabled):
                raise ProgressivePlanError("resume_planning_revision_mode_mismatch")
        else:
            state = {
                "schema_version": SCHEMA_VERSION,
                "topic_id": self.config.topic_id,
                "topic": topic,
                "pool_path": str(self.config.pool_path.resolve()),
                "plan_path": str(self.config.plan_path.resolve()),
                "pool_rows": len(pool_rows),
                "shared_deep_read_budget": self.config.shared_deep_read_budget,
                "planning_revision_enabled": bool(self.config.planning_revision_enabled),
                "completed_stages": [],
                "status": "in_progress",
            }
            _atomic_json(state_path, state)

        provisional_record = self._model_stage("provisional_scope", {
                "topic_id": self.config.topic_id,
                "research_question": topic,
                "original_plan": plan,
                "pool_row_count": len(b_pool),
                "candidate_pool": b_pool,
                "required_behavior": {"read_all_candidates": True, "candidate_pool_is_complete": True, "do_not_force_use": True},
            }, resume=resume, state=state)
        provisional = _stage_response(provisional_record)
        provisional = _resolve_planner_handles(provisional, source_handle_map)
        initial_theme_inventory = provisional.get("material_theme_inventory") or provisional.get("theme_inventory") or []

        def refresh_themes(*tool_results: Mapping[str, Any], routes: Mapping[str, Any] | None = None) -> list[Any]:
            inventory = _refresh_material_theme_inventory(initial_theme_inventory, initial_material_snapshot, pool_rows, self._read_materials)
            known_chapters = _outline_chapter_rows(provisional.get("provisional_outline") or [])
            for result in tool_results:
                known_chapters = _attach_chapter_candidate_sources(known_chapters, pool_rows, result)
            owners: dict[str, set[str]] = {}
            for chapter in known_chapters:
                for handle in chapter.get("source_handles") or []:
                    owners.setdefault(handle, set()).add(_text(chapter.get("chapter_id")))
            for route in (routes or {}).get("source_routes") or []:
                for chapter_id in route.get("chapter_ids") or []:
                    owners.setdefault(_text(route.get("source_handle")), set()).add(_text(chapter_id))
            for item in inventory:
                if isinstance(item, dict) and item.get("inventory_origin") == "material_update":
                    item["chapter_ids"] = sorted(owners.get(item["source_handle"]) or [])
            provisional["material_theme_inventory"] = inventory
            _atomic_json(root / "stages" / "material_theme_inventory.json", {"material_theme_inventory": inventory})
            return inventory

        level1_tool_result = self._tool_cycle(
            phase="level1",
            supplement_requests=provisional.get("supplement_requests") or [],
            directed_requests=provisional.get("directed_reads") or [],
            pool_rows=pool_rows,
            plan=plan,
            prior_directed=None,
            prior_tool_results=None,
            source_handle_map=source_handle_map,
            resume=resume,
            state=state,
        )
        pool_rows = _merge_supplement_pool_updates(pool_rows, level1_tool_result)
        source_handle_map = _refresh_source_handles(pool_rows)
        self._bind_tool_material_source_handles(pool_rows)
        paper_to_handle = {paper_id: handle for handle, paper_id in source_handle_map.items()}
        b_pool = [dict(row["_b_summary"]) for row in pool_rows]
        candidate_by_id = {str(row["_paper_id"]): row for row in pool_rows}
        refresh_themes(level1_tool_result)
        level1_outline_record = self._model_stage("level1_outline", {
                "topic_id": self.config.topic_id,
                "research_question": topic,
                "provisional_scope": provisional,
                "original_plan": _compact_original_plan(plan),
                "material_theme_inventory": provisional["material_theme_inventory"],
                "actual_tool_results": self._compact_tool_feedback(level1_tool_result),
                "full_b_pool_was_semantically_screened": len(b_pool),
                "unavailable_and_failed_tools_are_scope_feedback": True,
            }, resume=resume, state=state)
        level1_outline = _stage_response(level1_outline_record)
        if stop_after == "level1":
            partial = self._partial_result(topic, plan, provisional, level1_outline, level1_tool_result, "level1")
            _atomic_json(root / "PROGRESSIVE_REVIEW_PLAN.partial.json", partial)
            (root / "PROGRESSIVE_REVIEW_PLAN.partial.md").write_text(render_plan_markdown(partial), encoding="utf-8", newline="\n")
            state.update({"status": "stopped_after_level1", "current_stage": "", "output": str(root / "PROGRESSIVE_REVIEW_PLAN.partial.json")})
            _atomic_json(state_path, state)
            return partial

        state["topic"] = topic
        routing = self._route_sources(pool_rows, shared_outline=level1_outline.get("shared_outline"), resume=resume, state=state)
        limits_by_handle = {row["_source_handle"]: row["_b_summary"].get("scope_interpretation_cautions") or [] for row in pool_rows}
        for route in routing["source_routes"]:
            route["interpretation_limits"] = limits_by_handle.get(route.get("source_handle"), [])
        _atomic_json(root / "stages" / "source_routing_summary.json", routing)
        proposals_record = self._stage(
            "chapter_proposals",
            lambda: self._propose_chapters(topic=topic, outline=level1_outline, routing=routing, resume=resume),
            resume=resume,
            state=state,
            cache_inputs={"topic_id": self.config.topic_id, "research_question": topic,
                          "outline": level1_outline, "routing": routing,
                          "chapter_tool_materials": self.tool_materials_by_chapter},
        )
        proposals = self._attach_routed_sources(_normalize_proposal_response(_stage_response(proposals_record)), routing["source_routes"], level1_outline.get("shared_outline"))
        proposals = _resolve_planner_handles(proposals, source_handle_map)
        harmonize_record = self._model_stage("harmonized_scope", {
                "topic_id": self.config.topic_id,
                "research_question": topic,
                "shared_level1_outline": level1_outline,
                "chapter_proposals": _planner_scope_copy(proposals, paper_to_handle),
                "source_routing": routing["source_routes"],
                "level1_tool_results": self._compact_tool_feedback(level1_tool_result),
                "global_unique_deep_read_budget": self.config.shared_deep_read_budget,
            }, resume=resume, state=state)
        harmonized = _resolve_planner_handles(_stage_response(harmonize_record), source_handle_map)
        harmonized["chapters"] = self._harmonized_chapters(harmonized, proposals)
        level2_tool_result = self._tool_cycle(
            phase="level2",
            supplement_requests=harmonized.get("supplement_requests") or [],
            directed_requests=harmonized.get("directed_reads") or [],
            pool_rows=pool_rows,
            plan=plan,
            prior_directed=level1_tool_result,
            prior_tool_results=level1_tool_result,
            source_handle_map=source_handle_map,
            resume=resume,
            state=state,
        )
        pool_rows = _merge_supplement_pool_updates(pool_rows, level2_tool_result)
        source_handle_map = _refresh_source_handles(pool_rows)
        self._bind_tool_material_source_handles(pool_rows)
        paper_to_handle = {paper_id: handle for handle, paper_id in source_handle_map.items()}
        candidate_by_id = {str(row["_paper_id"]): row for row in pool_rows}
        refresh_themes(level1_tool_result, level2_tool_result, routes=routing)
        final_scope_record = self._model_stage("finalize_chapter_scope", {
                "topic_id": self.config.topic_id,
                "research_question": topic,
                **_review_guidance(level1_outline, harmonized),
                "material_theme_inventory": provisional["material_theme_inventory"],
                "harmonized_shared_outline": harmonized.get("shared_outline") or level1_outline.get("shared_outline") or {},
                "harmonized_chapters": (
                    _planner_scope_copy(harmonized, paper_to_handle).get("chapters")
                    or _planner_scope_copy(harmonized, paper_to_handle).get("harmonized_chapters")
                    or _planner_scope_copy(proposals, paper_to_handle).get("chapter_proposals")
                    or []
                ),
                "level2_tool_results": self._compact_tool_feedback(level2_tool_result),
                "new_and_upgraded_sources": [dict(row["_b_summary"]) for row in pool_rows if row.get("supplement_gap_material")],
                "do_not_issue_more_tool_requests": True,
            }, resume=resume, state=state)
        final_scope = _resolve_planner_handles(_stage_response(final_scope_record), source_handle_map)
        harmonized = {
            **harmonized,
            "shared_outline": final_scope.get("shared_outline") or harmonized.get("shared_outline"),
            "final_scope_notes": final_scope.get("final_scope_notes") or [],
            **{key: final_scope[key] for key in ("shared_scope", "review_argument") if final_scope.get(key)},
        }
        chapters = self._harmonized_chapters(final_scope, {"chapter_proposals": harmonized.get("chapters") or proposals.get("chapter_proposals") or []})
        if stop_after == "level2":
            partial = self._partial_result(topic, plan, provisional, level1_outline, level1_tool_result, "level2", harmonized=harmonized, level2_tools=level2_tool_result)
            _atomic_json(root / "PROGRESSIVE_REVIEW_PLAN.partial.json", partial)
            (root / "PROGRESSIVE_REVIEW_PLAN.partial.md").write_text(render_plan_markdown(partial), encoding="utf-8", newline="\n")
            state.update({"status": "stopped_after_level2", "current_stage": "", "output": str(root / "PROGRESSIVE_REVIEW_PLAN.partial.json")})
            _atomic_json(state_path, state)
            return partial

        chapter_gaps, chapter_directed = _chapter_retrieval_requests(chapters)
        if self.retrieval_loop_runner is not None:
            chapter_needs_record = self._model_stage("chapter_need_analysis", {
                    "topic_id": self.config.topic_id,
                    "research_question": topic,
                    "shared_outline": harmonized.get("shared_outline") or level1_outline.get("shared_outline") or {},
                    "chapters": [{key: row.get(key) for key in ("chapter_id", "title", "purpose", "scope", "substantive_threads")}
                                 for row in chapters],
                    "available_material": [{key: row.get(key) for key in ("source_handle", "chapter_ids", "specific_usable_material", "interpretation_limits")}
                                           for row in routing["source_routes"] if row.get("chapter_ids")],
                    "level1_tool_results": self._compact_tool_feedback(level1_tool_result),
                    "level2_tool_results": self._compact_tool_feedback(level2_tool_result),
                    "required_behavior": {"bounded_questions": True, "preserve_chapter_ids": True},
                }, resume=resume, state=state)
            chapter_needs = _resolve_planner_handles(_stage_response(chapter_needs_record), source_handle_map)
            raw_chapter_gaps = chapter_needs.get("supplement_requests") or chapter_needs.get("retrieval_gaps") or []
            raw_chapter_directed = chapter_needs.get("directed_reads") or chapter_needs.get("directed_read_requests") or []
            chapter_gaps.extend(_normalize_gaps([
                {
                    **dict(item),
                    "chapter_ids": list(dict.fromkeys([
                        *([item.get("chapter_id")] if item.get("chapter_id") else []),
                        *(([item.get("chapter_ids")] if isinstance(item.get("chapter_ids"), str) else list(item.get("chapter_ids") or []))),
                    ])),
                }
                for item in raw_chapter_gaps if isinstance(item, Mapping)
            ]))
            chapter_directed.extend(_merge_directed_tasks([
                {
                    **dict(item),
                    "chapter_ids": list(dict.fromkeys([
                        *([item.get("chapter_id")] if item.get("chapter_id") else []),
                        *(([item.get("chapter_ids")] if isinstance(item.get("chapter_ids"), str) else list(item.get("chapter_ids") or []))),
                    ])),
                }
                for item in raw_chapter_directed if isinstance(item, Mapping)
            ]))
        chapter_tool_result: dict[str, Any] = {"phase": "chapters", "supplement_results": [], "directed_results": []}
        if chapter_gaps or chapter_directed:
            chapter_tool_result = self._tool_cycle(
                phase="chapters",
                supplement_requests=chapter_gaps,
                directed_requests=chapter_directed,
                pool_rows=pool_rows,
                plan=plan,
                prior_directed=level2_tool_result,
                prior_tool_results=level2_tool_result,
                source_handle_map=source_handle_map,
                resume=resume,
                state=state,
            )
            pool_rows = _merge_supplement_pool_updates(pool_rows, chapter_tool_result)
            source_handle_map = _refresh_source_handles(pool_rows)
            candidate_by_id = {str(row.get("_paper_id")): row for row in pool_rows}
            self._bind_tool_material_source_handles(pool_rows)
            chapters = _attach_chapter_candidate_sources(chapters, pool_rows, chapter_tool_result)

        refresh_themes(level1_tool_result, level2_tool_result, chapter_tool_result, routes=routing)
        detail_records = self._chapter_details(
            chapters=chapters,
            shared_outline=harmonized.get("shared_outline") or level1_outline.get("shared_outline") or {},
            topic=topic,
            candidates=candidate_by_id,
            level1_tools=level1_tool_result,
            level2_tools=level2_tool_result,
            chapter_tools=chapter_tool_result,
            resume=resume,
            state=state,
            tool_materials_by_chapter=self.tool_materials_by_chapter,
            candidate_pool=pool_rows,
        )
        if self.config.recovery_from:
            detail_records, recovery_report = recover_compatible_chapter_details(
                detail_records,
                recovery_root=self.config.recovery_from,
                current_pool=pool_rows,
                shared_outline=harmonized.get("shared_outline") or level1_outline.get("shared_outline") or {},
                output_root=root,
                chapter_ids=self.config.recovery_chapters,
            )
            _atomic_json(root / "stages" / "chapter_recovery.json", recovery_report)
        # Late sources must reach the existing coordinator and their owner
        # before formal case attachment in either planning mode.
        baseline_detail_records = json.loads(json.dumps(detail_records, ensure_ascii=False, default=_json_default))
        routing = self._route_late_sources(
            routing, pool_rows, detail_records=detail_records,
            tool_results=[level2_tool_result, chapter_tool_result],
            shared_outline=harmonized.get("shared_outline") or level1_outline.get("shared_outline") or {},
            resume=resume, state=state,
        )
        detail_records = _attach_late_route_materials(detail_records, routing, pool_rows, self._read_materials)
        refresh_themes(level1_tool_result, level2_tool_result, chapter_tool_result, routes=routing)
        late_material_changes = _chapter_material_changes(detail_records, baseline_detail_records)
        whole_review_chapters = []
        for row in detail_records:
            chapter = row.get("chapter") if isinstance(row.get("chapter"), Mapping) else {}
            chapter_id = _text(chapter.get("chapter_id"))
            chapter_tool_materials = [
                _tool_material_for_prompt(item)
                for item in self.tool_materials_by_chapter.get(chapter_id, [])
                if isinstance(item, Mapping)
            ]
            whole_review_chapters.append({
                "chapter_id": chapter_id,
                "title": _text(chapter.get("title")),
                "scope": chapter.get("scope"),
                "chapter_plan": row.get("chapter_plan"),
                # The whole-plan pass gets only the A/B for handles actually
                # cited by this chapter, rather than the full routed pool.
                "source_materials": _chapter_review_source_materials(row),
                "tool_materials": chapter_tool_materials,
                "late_material_changes": late_material_changes.get(chapter_id, []),
            })
        whole_record = ({"response": {}}
                        if self.config.planning_revision_enabled else self._stage(
            "whole_plan_improvement",
            lambda: _call_record(self.planner, "whole_plan_improvement", {
                "topic_id": self.config.topic_id,
                "research_question": topic,
                "shared_outline": harmonized.get("shared_outline") or level1_outline.get("shared_outline") or {},
                "chapters": whole_review_chapters,
                "late_material_changes": late_material_changes,
                **_review_guidance(level1_outline, harmonized),
                "original_plan": _compact_original_plan(plan),
                "material_theme_inventory": provisional["material_theme_inventory"],
                "tool_results_summary": self._compact_tool_feedback({"level1": level1_tool_result, "level2": level2_tool_result, "chapters": chapter_tool_result}),
                "editorial_feedback": editorial_feedback,
                "citation_rules": dict(CURRENT_CITATION_RULES),
                "do_not_invent_evidence": True,
            }),
            resume=resume,
            state=state,
            cache_inputs={
                "chapters": whole_review_chapters,
                "late_material_changes": late_material_changes,
                **_review_guidance(level1_outline, harmonized),
                "original_plan": _compact_original_plan(plan),
                "material_theme_inventory": provisional["material_theme_inventory"],
                "tool_results_summary": self._compact_tool_feedback({"level1": level1_tool_result, "level2": level2_tool_result, "chapters": chapter_tool_result}),
                "editorial_feedback": editorial_feedback,
                "citation_rules": dict(CURRENT_CITATION_RULES),
            },
        ))
        improvement = dict(_stage_response(whole_record))
        improvement["late_material_changes"] = late_material_changes
        # The light whole-plan pass often returns useful prose adjustments but
        # leaves the chapter rewrite implicit.  Give named affected chapters a
        # bounded second call so those adjustments reach the writer packet as
        # concrete plans instead of remaining telemetry.
        editorial_feedback_entries = self._editorial_feedback_entries(editorial_feedback)
        affected_ids = list(dict.fromkeys([
            *self._affected_chapter_ids(improvement),
            *late_material_changes,
            *[_text(item.get("chapter_id")) for item in editorial_feedback_entries if _text(item.get("chapter_id"))],
        ]))
        concrete_ids = self._concrete_improvement_ids(improvement)
        editorial_ids = {_text(item.get("chapter_id")) for item in editorial_feedback_entries if _text(item.get("chapter_id"))}
        revision_ids = [chapter_id for chapter_id in affected_ids
                        if chapter_id not in concrete_ids or chapter_id in editorial_ids or chapter_id in late_material_changes]
        if revision_ids and not self.config.planning_revision_enabled:
            detail_by_id = {
                _text((row.get("chapter") or {}).get("chapter_id")): row
                for row in detail_records
                if isinstance(row, Mapping) and isinstance(row.get("chapter"), Mapping)
            }
            revision_root = root / "stages" / "affected_chapter_revision"
            revision_root.mkdir(parents=True, exist_ok=True)
            state.update({"status": "in_progress", "current_stage": "affected_chapter_revision"})
            _atomic_json(root / "RUN_STATE.json", state)
            common_revision_payload = {
                "topic_id": self.config.topic_id,
                "research_question": topic,
                **_review_guidance(level1_outline, harmonized, improvement),
                "improvement_notes": improvement.get("improvement_notes") or [],
                "cross_chapter_adjustments": improvement.get("cross_chapter_adjustments") or [],
                "editorial_feedback": editorial_feedback,
                "citation_rules": dict(CURRENT_CITATION_RULES),
                "required_behavior": {
                    "return_complete_updated_plan": True,
                    "preserve_source_handles_and_limits": True,
                    "do_not_add_evidence": True,
                },
            }
            if self.config.planning_revision_enabled:
                common_revision_payload["planning_revision_mode"] = True
            feedback_by_chapter: dict[str, list[dict[str, Any]]] = {}
            for item in self._improvement_entries(improvement):
                chapter_id = _text(item.get("chapter_id") or item.get("id"))
                if chapter_id:
                    feedback_by_chapter.setdefault(chapter_id, []).append(dict(item))
            for item in editorial_feedback_entries:
                chapter_id = _text(item.get("chapter_id"))
                if chapter_id:
                    feedback_by_chapter.setdefault(chapter_id, []).append(dict(item))
            review_chapter_by_id = {
                _text(item.get("chapter_id")): item
                for item in whole_review_chapters
                if _text(item.get("chapter_id"))
            }
            revision_inputs = {
                chapter_id: {
                    **common_revision_payload,
                    "call_id": f"affected-chapter-revision-{_safe_id(chapter_id)}",
                    "chapter_id": chapter_id,
                    "chapter": (detail_by_id.get(chapter_id) or {}).get("chapter") or {},
                    "chapter_plan": (detail_by_id.get(chapter_id) or {}).get("chapter_plan") or {},
                    "chapter_feedback": feedback_by_chapter.get(chapter_id) or [],
                    "late_material_changes": late_material_changes.get(chapter_id, []),
                    "editorial_feedback_for_chapter": [
                        dict(item) for item in editorial_feedback_entries
                        if _text(item.get("chapter_id")) == chapter_id
                    ],
                    "source_materials": (review_chapter_by_id.get(chapter_id) or {}).get("source_materials") or [],
                    "tool_materials": (review_chapter_by_id.get(chapter_id) or {}).get("tool_materials") or [],
                    **({
                        "candidate_navigation": (detail_by_id.get(chapter_id) or {}).get("candidate_navigation") or {},
                        "candidate_materials": (detail_by_id.get(chapter_id) or {}).get("candidate_materials") or [],
                    } if self.config.planning_revision_enabled else {}),
                    "affected_chapter": {
                        "chapter_id": chapter_id,
                        "chapter": (detail_by_id.get(chapter_id) or {}).get("chapter") or {},
                        "chapter_plan": (detail_by_id.get(chapter_id) or {}).get("chapter_plan") or {},
                    },
                }
                for chapter_id in revision_ids
                if chapter_id in detail_by_id
            }

            def revise_one(item: tuple[int, tuple[str, Mapping[str, Any]]]) -> tuple[int, dict[str, Any]]:
                index, (chapter_id, revision_payload) = item
                cache_path = revision_root / f"{_safe_id(chapter_id)}.json"
                if resume and cache_path.is_file():
                    try:
                        cached = _read_json(cache_path)
                        if (
                            isinstance(cached, Mapping)
                            and cached.get("status") == "complete"
                            and cached.get("cache_inputs") == revision_payload
                        ):
                            return index, dict(cached)
                    except ProgressivePlanError:
                        pass
                try:
                    record = _call_record(self.planner, "affected_chapter_revision", revision_payload)
                    response = _stage_response(record)
                    saved = {
                        "chapter_id": chapter_id,
                        "status": "complete",
                        "response": response,
                        "telemetry": record.get("telemetry") or {},
                        "cache_inputs": dict(revision_payload),
                    }
                except Exception as exc:
                    saved = {
                        "chapter_id": chapter_id,
                        "status": "failed",
                        "error": type(exc).__name__,
                        "error_detail": str(exc)[:1000],
                        "cache_inputs": dict(revision_payload),
                    }
                _atomic_json(cache_path, saved)
                return index, saved

            indexed_revisions: dict[int, dict[str, Any]] = {}
            revision_items = list(revision_inputs.items())
            with ThreadPoolExecutor(max_workers=max(1, self.config.chapter_workers)) as executor:
                futures = [executor.submit(revise_one, (index, item)) for index, item in enumerate(revision_items)]
                for future in as_completed(futures):
                    index, result = future.result()
                    indexed_revisions[index] = result
            revision_records = [indexed_revisions[index] for index in range(len(revision_items))]
            revision_response = {
                "chapter_updates": [
                    {
                        **dict(entry),
                        "chapter_id": entry.get("chapter_id") or record.get("chapter_id"),
                        "_complete_chapter_revision": True,
                    }
                    for record in revision_records
                    if record.get("status") == "complete"
                    for entry in self._improvement_entries(record.get("response") or {})
                    if isinstance(entry, Mapping)
                ],
                "chapter_revision_results": revision_records,
            }
            _atomic_json(root / "stages" / "affected_chapter_revision.json", {
                "response": revision_response,
                "chapter_ids": revision_ids,
                "cache_inputs": revision_inputs,
            })
            completed = list(state.get("completed_stages") or [])
            if "affected_chapter_revision" not in completed:
                completed.append("affected_chapter_revision")
            state.update({"completed_stages": completed, "current_stage": "", "status": "in_progress"})
            _atomic_json(root / "RUN_STATE.json", state)
            revision_entries = self._improvement_entries(revision_response)
            if revision_entries:
                improvement = dict(improvement)
                existing_updates = improvement.get("chapter_updates")
                if isinstance(existing_updates, Mapping):
                    existing_updates = [
                        ({**dict(value), "chapter_id": chapter_id} if isinstance(value, Mapping) else {"chapter_id": chapter_id, "adjustment": value})
                        for chapter_id, value in existing_updates.items()
                    ]
                elif not isinstance(existing_updates, list):
                    existing_updates = []
                improvement["chapter_updates"] = [*existing_updates, *revision_entries]
        detail_records = self._apply_improvements(detail_records, improvement)

        # BODY keeps the successful historical order: complete the global
        # coordination and owner-revision pass before case enrichment.  Case
        # selection is the final source/use append and must not trigger a
        # second BODY rewrite that can archive selected studies.
        if self.config.planning_revision_enabled:
            detail_records, improvement, _ = self._post_case_review(
                root=root,
                topic=topic,
                harmonized=harmonized,
                level1_outline=level1_outline,
                detail_records=detail_records,
                baseline_detail_records=baseline_detail_records,
                case_record={"response": {}},
                level1_tool_result=level1_tool_result,
                level2_tool_result=level2_tool_result,
                chapter_tool_result=chapter_tool_result,
                editorial_feedback=editorial_feedback,
                original_plan=plan,
                material_theme_inventory=provisional.get("material_theme_inventory") or provisional.get("theme_inventory") or [],
                pool_rows=pool_rows,
                resume=resume,
                state=state,
            )

        # Case enrichment is chapter-scoped because the full source-routing
        # ledger can be much larger than a single model context.  Every chapter
        # still gets a call, but each call receives only its bounded unit slice
        # and the routes relevant to that chapter.
        routing = self._route_late_sources(
            routing, pool_rows, detail_records=detail_records,
            tool_results=[level2_tool_result, chapter_tool_result],
            shared_outline=harmonized.get("shared_outline") or level1_outline.get("shared_outline") or {},
            resume=resume, state=state,
        )
        # A fresh process can reuse legacy tool-stage records without
        # executing the reader callback that populated _read_materials. Keep
        # those already-returned readings available to the case consumer too;
        # current in-memory readings take precedence over restored history.
        case_read_materials = {
            _text(row.get("paper_id")): dict(row)
            for row in [
                *self.prior_readings,
                *self._result_paper_materials(level1_tool_result),
                *self._result_paper_materials(level2_tool_result),
                *self._result_paper_materials(chapter_tool_result),
                *self._read_materials.values(),
            ]
            if isinstance(row, Mapping) and _text(row.get("paper_id"))
        }
        case_catalog = _case_unit_catalog(detail_records)
        chapter_ids = list(dict.fromkeys(
            _text((record.get("chapter") or {}).get("chapter_id"))
            for record in detail_records
            if isinstance(record, Mapping) and _text((record.get("chapter") or {}).get("chapter_id"))
        ))

        def run_case_groups() -> dict[str, Any]:
            case_root = self.config.output_dir / "stages" / "case_groups"
            case_root.mkdir(parents=True, exist_ok=True)
            chapter_results: list[dict[str, Any]] = []
            all_additions: list[dict[str, Any]] = []

            def merge_additions(additions: Sequence[Mapping[str, Any]]) -> None:
                """Merge batch responses without dropping distinct studies for one unit."""
                by_unit = {
                    _text(item.get("unit_key")): item
                    for item in all_additions
                    if isinstance(item, Mapping) and _text(item.get("unit_key"))
                }
                for raw in additions:
                    if not isinstance(raw, Mapping):
                        continue
                    item = dict(raw)
                    unit_key = _text(item.get("unit_key"))
                    if not unit_key:
                        continue
                    previous = by_unit.get(unit_key)
                    if previous is None:
                        all_additions.append(item)
                        by_unit[unit_key] = item
                        continue
                    existing_studies = previous.setdefault("studies", [])
                    seen_studies = {
                        (
                            "source",
                            _text(study.get("source_handle")) or _text(study.get("paper_id"))
                        ) if (_text(study.get("source_handle")) or _text(study.get("paper_id"))) else (
                            "raw",
                            json.dumps(dict(study), ensure_ascii=False, sort_keys=True, default=_json_default),
                        )
                        for study in existing_studies if isinstance(study, Mapping)
                    }
                    for study in item.get("studies") or []:
                        if not isinstance(study, Mapping):
                            continue
                        source_key = _text(study.get("source_handle")) or _text(study.get("paper_id"))
                        key = (
                            ("source", source_key)
                            if source_key else
                            ("raw", json.dumps(dict(study), ensure_ascii=False, sort_keys=True, default=_json_default))
                        )
                        if key not in seen_studies:
                            existing_studies.append(dict(study))
                            seen_studies.add(key)

            for chapter_id in chapter_ids:
                unit_rows = [row for row in case_catalog if row.get("chapter_id") == chapter_id]
                relevant_routes = [
                    {
                        key: route.get(key)
                        for key in ("source_handle", "paper_title", "chapter_ids", "specific_usable_material", "interpretation_limits")
                        if key in route
                    }
                    for route in routing["source_routes"]
                    if isinstance(route, Mapping) and chapter_id in (route.get("chapter_ids") or [])
                ]
                route_batches = [
                    relevant_routes[index:index + CASE_GROUP_ROUTE_BATCH_SIZE]
                    for index in range(0, len(relevant_routes), CASE_GROUP_ROUTE_BATCH_SIZE)
                ] or [[]]
                chapter_additions: list[dict[str, Any]] = []
                chapter_errors: list[str] = []
                chapter_telemetry: list[dict[str, Any]] = []
                route_handles = [_text(route.get("source_handle")) for route in relevant_routes]

                def update_unit_catalog(additions: Sequence[Mapping[str, Any]]) -> None:
                    """Carry accepted case sources into later batches for this chapter."""
                    by_key = {
                        _text(row.get("unit_key")): row
                        for row in unit_rows
                        if isinstance(row, Mapping) and _text(row.get("unit_key"))
                    }
                    for addition in additions:
                        target = by_key.get(_text(addition.get("unit_key"))) if isinstance(addition, Mapping) else None
                        if not isinstance(target, dict):
                            continue
                        unit = target.get("unit")
                        if not isinstance(unit, dict):
                            unit = {}
                            target["unit"] = unit
                        handles = [
                            _text(value) for value in (unit.get("source_handles") or []) if _text(value)
                        ]
                        prior_key = "supporting_studies"
                        prior_studies = unit.setdefault(prior_key, [])
                        for study in addition.get("studies") or []:
                            if not isinstance(study, Mapping):
                                continue
                            handle = _text(study.get("source_handle"))
                            if handle and handle not in handles:
                                handles.append(handle)
                            if handle and not any(_text(item.get("source_handle")) == handle for item in prior_studies):
                                prior_studies.append(dict(study))
                        if handles:
                            unit["source_handles"] = list(dict.fromkeys(handles))

                for batch_index, route_batch in enumerate(route_batches, start=1):
                    batch_id = f"{_safe_id(chapter_id)}__batch_{batch_index:03d}_of_{len(route_batches):03d}"
                    cache_path = case_root / f"{batch_id}.json"
                    batch_handles = [_text(route.get("source_handle")) for route in route_batch]
                    batch_unit_rows = [
                        {**dict(row), "unit": dict(row.get("unit") or {})}
                        for row in unit_rows
                    ]
                    unit_source_signature = [
                        {
                            "unit_key": _text(row.get("unit_key")),
                            "source_handles": [
                                _text(value)
                                for value in ((row.get("unit") or {}).get("source_handles") or [])
                                if _text(value)
                            ],
                        }
                        for row in batch_unit_rows
                    ]
                    batch_material_rows = _case_selection_material_rows(
                        batch_handles, detail_records, pool_rows, case_read_materials)
                    material_signature = hashlib.sha256(json.dumps(
                        batch_material_rows, ensure_ascii=False, sort_keys=True, default=_json_default
                    ).encode("utf-8")).hexdigest()[:16]
                    # The batch consumes the unit TASKS (points, briefs,
                    # cases) and the prompt contract, not just the handle
                    # list: a changed task or contract must not reuse an old
                    # answer.
                    task_signature = _case_unit_task_signature(batch_unit_rows, context={
                        "topic_id": self.config.topic_id,
                        "research_question": topic,
                        "source_routing": route_batch,
                        "source_routing_total": len(relevant_routes),
                        "batch_count": len(route_batches),
                        "pool_sources": len(pool_rows),
                        "review_sources_in_unit_catalog": len(set(re.findall(r"\bP\d{4,}\b", json.dumps(case_catalog, ensure_ascii=False)))),
                        "planning_revision_mode": self.config.planning_revision_enabled,
                    })
                    payload = {
                        "call_id": f"case-groups-{_safe_id(chapter_id)}-batch-{batch_index:03d}-of-{len(route_batches):03d}",
                        "topic_id": self.config.topic_id,
                        "research_question": topic,
                        "chapter_id": chapter_id,
                        "unit_catalog": batch_unit_rows,
                        "source_routing": route_batch,
                        "source_routing_total": len(relevant_routes),
                        "source_routing_batch_index": batch_index,
                        "source_routing_batch_count": len(route_batches),
                        "review_sources_in_unit_catalog": len(set(re.findall(r"\bP\d{4,}\b", json.dumps(case_catalog, ensure_ascii=False)))),
                        "pool_sources": len(pool_rows),
                        "breadth_rule": {
                            "goal": "整篇综述对主题、背景、发展与代表案例的充分覆盖",
                            "basis": "当前材料池规模与各单元实际材料",
                            "not_a_quota": "不为数量塞入无关文献，也不把只用少数核心论文当统一规则",
                        },
                        "output_contract": {
                            "chapter_id": chapter_id,
                            "bounded_unit_count": len(unit_rows),
                            "bounded_route_count": len(route_batch),
                            "total_relevant_route_count": len(relevant_routes),
                            "batch_index": batch_index,
                            "batch_count": len(route_batches),
                            "one_addition_per_relevant_unit": True,
                        },
                    }
                    # Selection support: the real material of exactly this
                    # batch's routed candidates, so the case model judges from
                    # content instead of inventing experiments for handles.
                    if batch_material_rows:
                        payload["source_materials"] = batch_material_rows
                    if self.config.planning_revision_enabled:
                        payload["planning_revision_mode"] = True
                    input_contract = self._cache_contract("case_groups", payload)
                    cached: Mapping[str, Any] | None = None
                    if resume and cache_path.is_file():
                        try:
                            candidate = _read_json(cache_path)
                            if (
                                isinstance(candidate, Mapping)
                                and candidate.get("status") == "complete"
                                and _text(candidate.get("chapter_id")) == chapter_id
                                and int(candidate.get("batch_index") or 0) == batch_index
                                and list(candidate.get("route_handles") or []) == batch_handles
                                and list(candidate.get("unit_source_signature") or []) == unit_source_signature
                                and _text(candidate.get("input_contract")) == input_contract
                                and _text(candidate.get("prompt_contract")) == CASE_GROUPS_PROMPT_CONTRACT
                            ):
                                cached = candidate
                        except (ProgressivePlanError, TypeError, ValueError):
                            cached = None
                    if cached is not None:
                        additions = [
                            dict(item) for item in cached.get("additions") or []
                            if isinstance(item, Mapping) and _text(item.get("unit_key")).startswith(chapter_id + ":")
                        ]
                        chapter_additions.extend(additions)
                        chapter_telemetry.append(dict(cached.get("telemetry") or {}))
                        update_unit_catalog(additions)
                        merge_additions(additions)
                        continue

                    try:
                        response_record = _call_record(self.planner, "case_groups", payload)
                        response = _stage_response(response_record)
                        additions = [
                            dict(item) for item in response.get("additions") or []
                            if isinstance(item, Mapping) and _text(item.get("unit_key")).startswith(chapter_id + ":")
                        ]
                        _atomic_json(cache_path, {
                            "status": "complete",
                            "chapter_id": chapter_id,
                            "batch_index": batch_index,
                            "batch_count": len(route_batches),
                            "route_handles": batch_handles,
                            "unit_source_signature": unit_source_signature,
                            "material_signature": material_signature,
                            "task_signature": task_signature,
                            "prompt_contract": CASE_GROUPS_PROMPT_CONTRACT,
                            "input_contract": input_contract,
                            "additions": additions,
                            "telemetry": response_record.get("telemetry") or {},
                        })
                        chapter_additions.extend(additions)
                        chapter_telemetry.append(response_record.get("telemetry") or {})
                        update_unit_catalog(additions)
                        merge_additions(additions)
                    except Exception as exc:
                        error = type(exc).__name__
                        chapter_errors.append(error)
                        _atomic_json(cache_path, {
                            "status": "failed",
                            "chapter_id": chapter_id,
                            "batch_index": batch_index,
                            "batch_count": len(route_batches),
                            "route_handles": batch_handles,
                            "unit_source_signature": unit_source_signature,
                            "material_signature": material_signature,
                            "task_signature": task_signature,
                            "prompt_contract": CASE_GROUPS_PROMPT_CONTRACT,
                            "input_contract": input_contract,
                            "error": error,
                        })
                result = {
                    "chapter_id": chapter_id,
                    "additions": chapter_additions,
                    "route_count": len(relevant_routes),
                    "route_batches": len(route_batches),
                    "processed_route_handles": route_handles,
                    "telemetry": chapter_telemetry[0] if len(chapter_telemetry) == 1 else chapter_telemetry,
                }
                if chapter_errors:
                    result["errors"] = chapter_errors
                    result["error"] = chapter_errors[0]
                chapter_results.append(result)
            return {
                "additions": all_additions,
                "chapter_results": chapter_results,
                "bounded_per_chapter": True,
                "status": "partial" if any(item.get("error") for item in chapter_results) else "complete",
            }

        case_record = self._stage(
            # Batch caches inside run_case_groups decide which successful calls
            # can be reused.  The stage wrapper must execute the collector on
            # resume so failed batches get retried without repeating successes.
            "case_groups", run_case_groups, resume=False, state=state,
            cache_inputs={
                "unit_catalog": case_catalog,
                "chapter_ids": chapter_ids,
                "source_routing": [
                    {
                        key: route.get(key)
                        for key in ("source_handle", "chapter_ids", "specific_usable_material", "interpretation_limits")
                        if key in route
                    }
                    for route in routing["source_routes"]
                    if isinstance(route, Mapping) and _text(route.get("source_handle"))
                ],
            },
        )
        baseline_detail_records = detail_records
        detail_records = _attach_case_groups(
            detail_records, _stage_response(case_record),
            planning_revision=self.config.planning_revision_enabled,
            body_case_additions=self.config.planning_revision_enabled,
            candidate_rows=pool_rows,
            read_materials=case_read_materials,
        )
        plan_output = self._assemble_final(
            topic=topic,
            plan=plan,
            pool_rows=pool_rows,
            provisional=provisional,
            level1_outline=level1_outline,
            harmonized=harmonized,
            chapters=chapters,
            chapter_records=detail_records,
            level1_tools=level1_tool_result,
            level2_tools=level2_tool_result,
            improvement=improvement,
            chapter_tools=chapter_tool_result,
        )
        plan_output["case_enrichment"] = _stage_response(case_record)
        if plan_output["case_enrichment"].get("status") in {"partial", "not_run_budget"}:
            plan_output["status"] = "initial_draft"
        self._write_final_outputs(plan_output)
        state.update({"status": plan_output["status"], "current_stage": "", "output": str(root / "DETAILED_REVIEW_PLAN.json"), "completed_chapters": len(detail_records)})
        _atomic_json(state_path, state)
        return plan_output

    @staticmethod
    def _compact_tool_feedback(value: Mapping[str, Any]) -> dict[str, Any]:
        def compact(result: Any) -> Any:
            if not isinstance(result, Mapping):
                return result
            keys = (
                "status", "error", "reason", "phase", "gap_id", "gap_question", "substantive_gap_status",
                "fulfillment_judgment", "source_units", "citation_anchors", "materials", "papers",
                 "consumed_paper_ids", "reused_prior_tasks", "deferred_tasks", "requested_unique_papers",
                 "selected_unique_papers", "shared_budget_remaining", "results", "output_dir", "outcome",
                 "local_triage", "retrieval_loop", "owner_content", "tool_materials_by_chapter",
                 "chapter_ids", "question", "usable_content", "useful_material", "auxiliary_material",
                 "remaining_gap", "still_missing", "limitations", "outline_action", "derived_pool_path",
            )
            out = {key: result[key] for key in keys if key in result}
            if isinstance(result.get("materials"), list):
                out["materials"] = [_compact_reading_material(row) for row in result["materials"]]
                # Per-paper results repeat the same complete reading artifacts.
                out["results"] = [{key: row[key] for key in ("paper_id", "status", "reason", "error") if key in row}
                                  for row in result.get("results") or [] if isinstance(row, Mapping)]
            elif isinstance(result.get("results"), list):
                out["results"] = [compact(row) for row in result["results"]]
            if isinstance(result.get("source_units"), list):
                out["source_units"] = [{key: row[key] for key in ("record_identity", "material_depth", "status", "fulfillment_judgment") if key in row}
                                       for row in result["source_units"] if isinstance(row, Mapping)]
            return out
        output: dict[str, Any] = {}
        for key in ("supplement_results", "directed_results"):
            raw = value.get(key)
            if isinstance(raw, list):
                output[key] = [compact(item) for item in raw]
        if "phase" in value:
            output["phase"] = value["phase"]
        if "consumed_paper_ids" in value:
            output["consumed_paper_ids"] = list(value.get("consumed_paper_ids") or [])
        if "status" in value:
            output["status"] = value["status"]
        # Local-only answers do not occur in either provider-result array.
        # Reuse the source-bound writer projection, not the lookup passages,
        # complete pool, or attempt journal, for the next outline consumer.
        materials = value.get("tool_materials_by_chapter")
        if isinstance(materials, Mapping):
            output["tool_materials_by_chapter"] = {
                str(chapter_id): [_tool_material_for_prompt(row) for row in rows if isinstance(row, Mapping)]
                for chapter_id, rows in materials.items() if isinstance(rows, (list, tuple))
            }
        loop = value.get("retrieval_loop")
        if isinstance(loop, Mapping):
            output["retrieval_loop"] = {"needs": [
                {key: row[key] for key in ("need_id", "status", "action", "still_missing") if key in row}
                for row in loop.get("needs") or [] if isinstance(row, Mapping)
            ]}
        for key in ("level1", "level2", "chapters", "chapter_tools"):
            if isinstance(value.get(key), Mapping):
                output[key] = ProgressiveReviewPlanner._compact_tool_feedback(value[key])
        return output

    def _post_case_review(
        self,
        *,
        root: Path,
        topic: str,
        harmonized: Mapping[str, Any],
        level1_outline: Mapping[str, Any],
        detail_records: Sequence[Mapping[str, Any]],
        baseline_detail_records: Sequence[Mapping[str, Any]] | None = None,
        case_record: Mapping[str, Any],
        level1_tool_result: Mapping[str, Any],
        level2_tool_result: Mapping[str, Any],
        chapter_tool_result: Mapping[str, Any],
        editorial_feedback: Mapping[str, Any],
        original_plan: Mapping[str, Any] | None = None,
        material_theme_inventory: Sequence[Mapping[str, Any]] | Sequence[str] = (),
        pool_rows: Sequence[Mapping[str, Any]] = (),
        resume: bool,
        state: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
        """Run the opt-in global/revision pass before case enrichment.

        The ordinary mode keeps its historical order.  This narrow path gives
        late material to the same whole-plan and chapter-owner roles before
        case enrichment, then returns updated plans for final arrangement; it
        does not create a second planner or a writer-side scientific authority.
        """

        case_response = _stage_response(case_record)
        # Refresh authoritative local cards before case work.  The case model
        # contributes pointers and uses; it cannot author or overwrite A/B.
        detail_records = _refresh_local_material_snapshots(detail_records)
        for record in detail_records:
            _refresh_owner_packet_materials(
                record, pool_rows=pool_rows, deep_material_by_paper=self._read_materials,
            )
        by_chapter_material = _chapter_material_changes(detail_records, baseline_detail_records, case_response)

        whole_review_chapters: list[dict[str, Any]] = []
        for row in detail_records:
            chapter = row.get("chapter") if isinstance(row.get("chapter"), Mapping) else {}
            chapter_id = _text(chapter.get("chapter_id"))
            whole_review_chapters.append({
                "chapter_id": chapter_id,
                "title": _text(chapter.get("title")),
                "scope": chapter.get("scope"),
                "chapter_plan": row.get("chapter_plan"),
                "source_materials": _chapter_review_source_materials(row),
                "tool_materials": [
                    _tool_material_for_prompt(item)
                    for item in self.tool_materials_by_chapter.get(chapter_id, [])
                    if isinstance(item, Mapping)
                ],
                "late_material_changes": by_chapter_material.get(chapter_id, []),
            })
        whole_inputs = {
            "chapters": whole_review_chapters,
            "original_plan": _compact_original_plan(original_plan or {}),
            **_review_guidance(level1_outline, harmonized),
            "material_theme_inventory": [
                dict(item) if isinstance(item, Mapping) else _text(item)
                for item in (material_theme_inventory or ())
            ],
            "useful_unassigned_navigation": [
                {
                    key: item.get(key)
                    for key in ("source_handle", "paper_id", "title", "material_depth", "assignment", "material_available", "retrieval")
                    if key in item
                }
                for row in detail_records
                for item in ((row.get("candidate_navigation") or {}).get("candidates") or ())
                if isinstance(item, Mapping) and not bool((item.get("assignment") or {}).get("selected_in_chapter"))
            ],
            # Same paper serving several chapters is legitimate when the uses
            # differ; the coordinator sees the actual overlap and judges by
            # role instead of mechanically de-duplicating citations.
            "cross_chapter_source_uses": _cross_chapter_source_uses(detail_records),
            "tool_results_summary": self._compact_tool_feedback({"level1": level1_tool_result, "level2": level2_tool_result, "chapters": chapter_tool_result}),
            "tool_feedback_scope": {
                "unanswered_need_means": "当前池未取得该内容，仅约束受影响章节本次可写结论；不构成领域缺失或不存在更优路线的证据",
            },
            "editorial_feedback": editorial_feedback,
            "citation_rules": dict(CURRENT_CITATION_RULES),
            "late_material_changes": by_chapter_material,
            "planning_revision_mode": True,
        }
        whole_record = self._stage(
            "whole_plan_improvement",
            lambda: _call_record(self.planner, "whole_plan_improvement", {
                "topic_id": self.config.topic_id,
                "research_question": topic,
                "shared_outline": harmonized.get("shared_outline") or level1_outline.get("shared_outline") or {},
                **whole_inputs,
                "do_not_invent_evidence": True,
                "required_behavior": {
                    "review_late_material_content": True,
                    "ordinary_or_supplement_label_does_not_skip_review": True,
                    "return_only_bounded_cross_chapter_changes": True,
                },
            }),
            resume=resume,
            state=state,
            cache_inputs=whole_inputs,
        )
        improvement = _stage_response(whole_record)
        improvement = dict(improvement)
        improvement["late_material_changes"] = by_chapter_material
        improvement["material_theme_inventory"] = whole_inputs["material_theme_inventory"]
        # In the opt-in path the whole-plan response is feedback for the
        # chapter owner.  Keeping its scalar suggestions in ``improvement``
        # is useful for the audit packet, but applying them before the owner
        # returns a complete plan would let a failed/no-change owner call
        # silently rewrite the authoritative chapter.
        global_feedback_entries = self._improvement_entries(improvement)
        if global_feedback_entries:
            improvement["owner_revision_feedback"] = [dict(item) for item in global_feedback_entries]
        editorial_entries = self._editorial_feedback_entries(editorial_feedback)
        # Pending case suggestions are a new use of a paper for this chapter,
        # even when the underlying A/B material is unchanged; the owner must
        # judge the proposal against the real content.
        case_suggestions_by_chapter: dict[str, list[dict[str, Any]]] = {}
        for record in detail_records:
            chapter = record.get("chapter") if isinstance(record.get("chapter"), Mapping) else {}
            chapter_id = _text(chapter.get("chapter_id"))
            if not chapter_id:
                continue
            for index, unit in enumerate(_chapter_units(record.get("chapter_plan") or {})):
                if not isinstance(unit, Mapping):
                    continue
                pending = [dict(item) for item in (unit.get("case_suggestions") or [])
                           if isinstance(item, Mapping)]
                if pending:
                    case_suggestions_by_chapter.setdefault(chapter_id, []).append({
                        "unit_key": f"{chapter_id}:{index + 1}",
                        "unit_point": _text(unit.get("substantive_point") or unit.get("point")),
                        "studies": pending,
                    })
        affected_ids = list(dict.fromkeys([
            *self._affected_chapter_ids(improvement),
            *by_chapter_material,
            *case_suggestions_by_chapter,
            *[_text(item.get("chapter_id")) for item in editorial_entries if _text(item.get("chapter_id"))],
        ]))
        # In the opt-in path every affected chapter is returned to its owner.
        # A short scalar replacement is still a scientific change when it
        # touches thesis/scope; late material is never bypassed by a label.
        revision_ids = affected_ids
        reviewed_chapter_ids: set[str] = set()

        if revision_ids:
            detail_by_id = {
                _text((row.get("chapter") or {}).get("chapter_id")): row
                for row in detail_records
                if isinstance(row, Mapping) and isinstance(row.get("chapter"), Mapping)
            }
            revision_root = root / "stages" / "affected_chapter_revision"
            revision_root.mkdir(parents=True, exist_ok=True)
            state.update({"status": "in_progress", "current_stage": "affected_chapter_revision"})
            _atomic_json(root / "RUN_STATE.json", state)
            feedback_by_chapter: dict[str, list[dict[str, Any]]] = {}
            for item in self._improvement_entries(improvement):
                chapter_id = _text(item.get("chapter_id") or item.get("id"))
                if chapter_id:
                    feedback_by_chapter.setdefault(chapter_id, []).append(dict(item))
            for item in editorial_entries:
                chapter_id = _text(item.get("chapter_id"))
                if chapter_id:
                    feedback_by_chapter.setdefault(chapter_id, []).append(dict(item))
            review_chapter_by_id = {
                _text(item.get("chapter_id")): item
                for item in whole_review_chapters
                if _text(item.get("chapter_id"))
            }
            owner_source_materials_by_chapter: dict[str, list[dict[str, Any]]] = {}
            owner_material_resolution_by_chapter: dict[str, dict[str, Any]] = {}
            for chapter_id in revision_ids:
                record = detail_by_id.get(chapter_id) or {}
                owner_sources, resolution = _resolve_owner_source_materials(
                    source_materials=record.get("source_materials") or [],
                    chapter_plan=record.get("chapter_plan") if isinstance(record.get("chapter_plan"), Mapping) else {},
                    chapter_feedback=[
                        *(feedback_by_chapter.get(chapter_id) or []),
                        *(case_suggestions_by_chapter.get(chapter_id) or []),
                    ],
                    candidate_navigation=record.get("candidate_navigation") if isinstance(record.get("candidate_navigation"), Mapping) else {},
                    candidate_materials=record.get("candidate_materials") or [],
                    pool_rows=pool_rows,
                    deep_material_by_paper=self._read_materials,
                )
                owner_source_materials_by_chapter[chapter_id] = owner_sources
                owner_material_resolution_by_chapter[chapter_id] = resolution
                # Keep the resolved material in the authoritative in-memory
                # packet so a successful owner revision reaches downstream
                # arrangement/writing inputs.
                record["source_materials"] = owner_sources
            revision_inputs = {
                chapter_id: {
                    "topic_id": self.config.topic_id,
                    "research_question": topic,
                    "planning_revision_mode": True,
                    **_review_guidance(level1_outline, harmonized, improvement),
                    "call_id": f"affected-chapter-revision-{_safe_id(chapter_id)}",
                    "chapter_id": chapter_id,
                    "chapter": (detail_by_id.get(chapter_id) or {}).get("chapter") or {},
                    "chapter_plan": _seed_owner_unit_ids(
                        (detail_by_id.get(chapter_id) or {}).get("chapter_plan") or {}, {}
                    )[0],
                    "chapter_feedback": feedback_by_chapter.get(chapter_id) or [],
                    "editorial_feedback_for_chapter": [
                        dict(item) for item in editorial_entries
                        if _text(item.get("chapter_id")) == chapter_id
                    ],
                    # Case-layer selection proposals: uses the owner should
                    # adopt as concrete cases from the real material, or
                    # reject.  A proposal is never the paper's own finding.
                    "case_suggestions": case_suggestions_by_chapter.get(chapter_id) or [],
                    # The owner reviews the plan against the material it must
                    # correct, including compacted paid deep reads; the
                    # whole-plan coordinator keeps its compact shape.
                    "source_materials": _chapter_review_source_materials(
                        {"chapter_plan": {
                            "plan": (detail_by_id.get(chapter_id) or {}).get("chapter_plan") or {},
                            "selected_feedback": feedback_by_chapter.get(chapter_id) or [],
                            "case_suggestions": case_suggestions_by_chapter.get(chapter_id) or [],
                         }, "source_materials": owner_source_materials_by_chapter.get(chapter_id) or []},
                        include_deep_read=True),
                    "owner_material_resolution": owner_material_resolution_by_chapter.get(chapter_id) or {},
                    "late_material_changes": by_chapter_material.get(chapter_id, []),
                    "tool_materials": (review_chapter_by_id.get(chapter_id) or {}).get("tool_materials") or [],
                    "candidate_navigation": (detail_by_id.get(chapter_id) or {}).get("candidate_navigation") or {},
                    "candidate_materials": (detail_by_id.get(chapter_id) or {}).get("candidate_materials") or [],
                    "unit_identity_contract": {
                        "existing_unit_ids": _seed_owner_unit_ids(
                            (detail_by_id.get(chapter_id) or {}).get("chapter_plan") or {}, {}
                        )[1],
                        "new_unit_requires_explicit_unit_id": True,
                        "split_or_merge_requires_unit_id_remap": True,
                        "unit_id_remap_shape": "{new_unit_id: [old_unit_id, ...]}",
                    },
                    "required_behavior": {
                        "return_complete_updated_plan": True,
                        "review_original_and_incremental_material_together": True,
                        "preserve_source_handles_and_limits": True,
                        "allow_no_material_change": True,
                        "do_not_add_evidence": True,
                    },
                }
                for chapter_id in revision_ids
                if chapter_id in detail_by_id
            }

            def revise_one(item: tuple[int, tuple[str, Mapping[str, Any]]]) -> tuple[int, dict[str, Any]]:
                index, (chapter_id, revision_payload) = item
                cache_path = revision_root / f"{_safe_id(chapter_id)}.json"
                success_path = revision_root / "successful" / cache_path.name
                projection = _owner_cache_projection(revision_payload)
                owner_contract = self._cache_contract("affected_chapter_revision", projection)
                compatible_success = None
                for path in (cache_path, success_path):
                    cached = _owner_success_record(path)
                    if (cached is None or _text(cached.get("chapter_id")) != chapter_id
                            or cached.get("owner_cache_contract") != owner_contract
                            or _owner_cache_projection(cached["cache_inputs"]) != projection):
                        continue
                    cached_materials, _ = _close_owner_response_materials(revision_payload, {"updated_plan": cached["updated_plan"]})
                    status, _, _, errors = _classify_owner_response(
                        revision_payload.get("chapter_plan") or {},
                        {"status": cached["owner_status"], "updated_plan": cached["updated_plan"],
                         "unit_id_remap": cached.get("unit_id_remap") or {}},
                        cached_materials, expected_chapter_id=chapter_id,
                    )
                    if status in {"updated", "no_change"} and not errors:
                        compatible_success = {**cached, "source_materials": cached_materials}
                        # Preserve this validated success separately before a
                        # later attempt can replace the latest-attempt record.
                        if path == cache_path:
                            _atomic_json(success_path, cached)
                        elif cache_path.is_file():
                            try:
                                latest_attempt = _read_json(cache_path)
                            except ProgressivePlanError:
                                latest_attempt = {}
                            if (isinstance(latest_attempt, Mapping)
                                    and latest_attempt.get("status") in {"failed", "partial"}
                                    and _owner_cache_projection(latest_attempt.get("cache_inputs")) == projection):
                                compatible_success["retained_compatible_success"] = True
                                compatible_success["latest_attempt_status"] = latest_attempt["status"]
                                compatible_success["latest_attempt_error"] = latest_attempt.get("error") or latest_attempt.get("structural_errors")
                        break
                if resume and compatible_success is not None:
                    return index, compatible_success
                try:
                    record = _call_record(self.planner, "affected_chapter_revision", revision_payload)
                    response = _stage_response(record)
                    adopted_materials, adoption_resolution = _close_owner_response_materials(revision_payload, response)
                    owner_status, updated_plan, unit_id_remap, structural_errors = _classify_owner_response(
                        revision_payload.get("chapter_plan") or {},
                        response,
                        adopted_materials,
                        expected_chapter_id=chapter_id,
                    )
                    saved = {
                        "chapter_id": chapter_id,
                        "status": "complete" if owner_status in {"updated", "no_change"} else "partial",
                        "owner_status": owner_status,
                        "source_materials": adopted_materials if owner_status in {"updated", "no_change"} else [],
                        "adoption_material_resolution": adoption_resolution,
                        "response": response,
                        "updated_plan": updated_plan,
                        "unit_id_remap": unit_id_remap,
                        "structural_errors": structural_errors,
                        "telemetry": record.get("telemetry") or {},
                        "cache_inputs": dict(revision_payload),
                    }
                except Exception as exc:
                    saved = {
                        "chapter_id": chapter_id,
                        "status": "failed",
                        "owner_status": "unresolved",
                        "error": type(exc).__name__,
                        "error_detail": str(exc)[:1000],
                        "cache_inputs": dict(revision_payload),
                    }
                saved["cache_projection"] = projection
                saved["cache_signature"] = _material_content_signature(projection)
                saved["owner_cache_contract"] = owner_contract
                if cache_path.is_file():
                    try:
                        previous_attempt = _read_json(cache_path)
                    except ProgressivePlanError:
                        previous_attempt = None
                    if isinstance(previous_attempt, Mapping):
                        # Keep prior attempts, including legacy successes whose
                        # prompt/model provenance cannot justify implicit reuse.
                        history_path = revision_root / "history" / _safe_id(chapter_id) / (
                            _material_content_signature(previous_attempt) + ".json")
                        _atomic_json(history_path, previous_attempt)
                        if _owner_success_record(cache_path) is not None and not success_path.is_file():
                            _atomic_json(success_path, previous_attempt)
                _atomic_json(cache_path, saved)
                if saved.get("status") == "complete":
                    _atomic_json(success_path, saved)
                elif compatible_success is not None:
                    retained = dict(compatible_success)
                    retained["retained_compatible_success"] = True
                    retained["latest_attempt_status"] = saved.get("status")
                    retained["latest_attempt_error"] = saved.get("error") or saved.get("structural_errors")
                    return index, retained
                return index, saved

            revision_items = list(revision_inputs.items())
            indexed: dict[int, dict[str, Any]] = {}
            with ThreadPoolExecutor(max_workers=max(1, self.config.chapter_workers)) as executor:
                futures = [executor.submit(revise_one, (index, item)) for index, item in enumerate(revision_items)]
                for future in as_completed(futures):
                    index, result = future.result()
                    indexed[index] = result
            revision_records = [indexed[index] for index in range(len(revision_items))]
            reviewed_chapter_ids.update(
                str(item.get("chapter_id"))
                for item in revision_records
                if item.get("status") == "complete"
            )
            for owner_record in revision_records:
                if owner_record.get("status") == "complete" and owner_record.get("source_materials") is not None:
                    packet = detail_by_id.get(_text(owner_record.get("chapter_id")))
                    if packet is not None:
                        packet["source_materials"] = owner_record["source_materials"]
                        identity_map = dict(packet.get("source_identity_map") or {})
                        for material in packet["source_materials"]:
                            if material.get("source_handle"):
                                identity_map[material["source_handle"]] = {
                                    **identity_map.get(material["source_handle"], {}),
                                    **{k: material.get(k) for k in ("paper_id", "title", "doi", "year") if material.get(k)},
                                }
                        packet["source_identity_map"] = identity_map
            revision_entries = [
                {"chapter_id": record.get("chapter_id"), "updated_plan": record["updated_plan"],
                 "unit_id_remap": record.get("unit_id_remap") or {}, "_complete_chapter_revision": True}
                for record in revision_records
                if record.get("status") == "complete" and isinstance(record.get("updated_plan"), Mapping)
            ]
            revision_response = {"chapter_updates": revision_entries, "chapter_revision_results": revision_records}
            _atomic_json(root / "stages" / "affected_chapter_revision.json", {
                "response": revision_response, "chapter_ids": revision_ids, "cache_inputs": revision_inputs,
            })
            improvement["chapter_updates"] = [dict(item) for item in revision_entries]
            improvement["chapter_revision_results"] = [dict(item) for item in revision_records]
            improvement["owner_revision_attempt_failures"] = [
                str(item.get("chapter_id")) for item in revision_records
                if item.get("latest_attempt_status") in {"failed", "partial"}
                or item.get("status") in {"failed", "partial"}
            ]
            improvement["owner_revision_unresolved"] = [
                str(item.get("chapter_id"))
                for item in revision_records
                if item.get("owner_status") not in {"updated", "no_change"}
            ]

        # Only a successful owner response can update the plan in this mode.
        # Whole-plan scalar entries remain visible as feedback above, while
        # explicit editorial acceptance can be handled by a caller before it
        # reaches this apply step.  This prevents failed/no-change owner
        # revisions from falling back to the original global suggestion.
        if self.config.planning_revision_enabled:
            apply_improvement = {
                "chapter_updates": [
                    dict(item) for item in (improvement.get("chapter_updates") or [])
                    if isinstance(item, Mapping) and item.get("_complete_chapter_revision") is True
                ],
            }
        else:
            apply_improvement = improvement
        final_records = self._apply_improvements(detail_records, apply_improvement)
        # A completed owner review has judged every pending case suggestion:
        # adopted ones live in the owner's plan, rejected ones are simply not
        # there.  The proposals move to a packet-level audit trail so they do
        # not re-trigger the owner and never read as established content.
        final_records = _archive_reviewed_case_suggestions(final_records, reviewed_chapter_ids)
        return final_records, improvement, whole_review_chapters

    @staticmethod
    def _attach_routed_sources(
        proposals: Mapping[str, Any],
        source_routes: Sequence[Mapping[str, Any]],
        shared_outline: Any,
    ) -> dict[str, Any]:
        """Make every semantically routed source survive chapter proposal editing."""
        result = json.loads(json.dumps(proposals, ensure_ascii=False, default=_json_default))
        rows = result.get("chapter_proposals") or []
        if isinstance(rows, Mapping):
            rows = [dict(rows)]
        if not isinstance(rows, list):
            rows = []
        route_handles: dict[str, list[str]] = {}
        for route in source_routes:
            if not isinstance(route, Mapping):
                continue
            handle = _text(route.get("source_handle"))
            for chapter_id in route.get("chapter_ids") or []:
                chapter_id = _text(chapter_id)
                if handle and chapter_id:
                    route_handles.setdefault(chapter_id, []).append(handle)
        existing_by_id = {
            _text(row.get("chapter_id") or row.get("id")): dict(row)
            for row in rows if isinstance(row, Mapping) and _text(row.get("chapter_id") or row.get("id"))
        }
        for chapter in _outline_chapter_rows(shared_outline):
            chapter_id = _text(chapter.get("chapter_id") or chapter.get("id"))
            if chapter_id and chapter_id not in existing_by_id:
                existing_by_id[chapter_id] = {
                    "chapter_id": chapter_id,
                    "title": _text(chapter.get("title") or chapter.get("chapter_title") or chapter_id),
                    "purpose": _text(chapter.get("purpose") or chapter.get("scope")),
                    "proposal_completion_note": "Expanded from the full-pool source-routing ledger.",
                }
        output_rows: list[dict[str, Any]] = []
        for chapter_id, row in existing_by_id.items():
            existing_handles = row.get("source_handles") or []
            if isinstance(existing_handles, str):
                existing_handles = [existing_handles]
            routed = route_handles.get(chapter_id, [])
            excluded_rows = row.get("excluded_source_handles") or []
            if any(isinstance(item, Mapping) for item in excluded_rows):
                row["source_exclusion_notes"] = excluded_rows
            row["excluded_source_handles"] = _source_values(excluded_rows)
            excluded = set(row["excluded_source_handles"])
            row["source_handles"] = [handle for handle in dict.fromkeys([*[_text(item) for item in existing_handles if _text(item)], *routed]) if handle not in excluded]
            output_rows.append(row)
        result["chapter_proposals"] = output_rows
        return result

    @staticmethod
    def _harmonized_chapters(harmonized: Mapping[str, Any], proposals: Mapping[str, Any]) -> list[dict[str, Any]]:
        proposal_rows = proposals.get("chapter_proposals") or proposals.get("chapters") or []
        source_rows = harmonized.get("chapters") or harmonized.get("harmonized_chapters") or []
        by_id = {str(row.get("chapter_id") or ""): dict(row) for row in source_rows if isinstance(row, Mapping)}
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in proposal_rows:
            if not isinstance(raw, Mapping):
                continue
            proposal = dict(raw)
            chapter_id = _text(proposal.get("chapter_id") or proposal.get("id"))
            if not chapter_id or chapter_id in seen:
                continue
            seen.add(chapter_id)
            shared = by_id.get(chapter_id)
            if shared:
                merged = {**proposal, **shared}
                proposal_sources = proposal.get("source_ids") or proposal.get("paper_ids") or []
                shared_sources = shared.get("source_ids") or shared.get("paper_ids") or []
                merged["source_ids"] = list(dict.fromkeys(
                    [_text(item) for item in [*proposal_sources, *shared_sources] if _text(item)]
                ))
                proposal_handles = proposal.get("source_handles") or []
                shared_handles = shared.get("source_handles") or []
                merged["source_handles"] = list(dict.fromkeys(
                    [_text(item) for item in [*proposal_handles, *shared_handles] if _text(item)]
                ))
            else:
                merged = proposal
            excluded_ids = set(_source_values(merged.get("excluded_source_ids")))
            excluded_handles = set(_source_values(merged.get("excluded_source_handles")))
            merged["source_ids"] = [item for item in merged.get("source_ids") or [] if item not in excluded_ids]
            merged["source_handles"] = [item for item in merged.get("source_handles") or [] if item not in excluded_handles]
            merged["chapter_id"] = chapter_id
            merged["title"] = _text(merged.get("title") or merged.get("chapter_title") or chapter_id)
            merged["source_ids"] = list(dict.fromkeys(_text(x) for x in (merged.get("source_ids") or merged.get("paper_ids") or []) if _text(x)))
            output.append(merged)
        if not output:
            raise ProgressivePlanError("chapter_proposals_empty")
        return output

    @staticmethod
    def _result_paper_materials(tool_result: Mapping[str, Any]) -> list[dict[str, Any]]:
        papers: list[dict[str, Any]] = []
        seen: set[str] = set()
        for key in ("supplement_results", "directed_results"):
            pending = list(tool_result.get(key) or [])
            while pending:
                group = pending.pop(0)
                if not isinstance(group, Mapping):
                    continue
                for item in group.get("materials") or group.get("papers") or []:
                    if isinstance(item, Mapping):
                        paper_id = _text(item.get("paper_id") or item.get("canonical_paper_id"))
                        if paper_id and paper_id in seen:
                            continue
                        if paper_id:
                            seen.add(paper_id)
                        papers.append(dict(item))
                pending.extend(group.get("results") or [])
        return papers

    def _chapter_details(
        self,
        *,
        chapters: Sequence[Mapping[str, Any]],
        shared_outline: Any,
        topic: str,
        candidates: Mapping[str, Mapping[str, Any]],
        level1_tools: Mapping[str, Any],
        level2_tools: Mapping[str, Any],
        resume: bool,
        state: dict[str, Any],
        chapter_tools: Mapping[str, Any] | None = None,
        tool_materials_by_chapter: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        candidate_pool: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        root = self.config.output_dir / "stages" / "chapters"
        root.mkdir(parents=True, exist_ok=True)
        routing_path = self.config.output_dir / "stages" / "source_routing_summary.json"
        routing_rows = (_read_json(routing_path).get("source_routes") or []) if routing_path.is_file() else []
        route_by_handle = {row.get("source_handle"): row for row in routing_rows}
        l1_materials = self._result_paper_materials(level1_tools)
        l2_materials = self._result_paper_materials(level2_tools)
        # Chapter-phase directed reads are real paid readings for this
        # chapter's own sources; harvesting only level1/level2 left them out of
        # every packet's deep_read_material.
        chapter_phase_materials = self._result_paper_materials(chapter_tools or {})
        all_materials = {str(row.get("paper_id")): row for row in [*l1_materials, *l2_materials, *chapter_phase_materials] if _text(row.get("paper_id"))}
        for row in self.prior_readings:
            if _text(row.get("paper_id")):
                all_materials.setdefault(_text(row.get("paper_id")), dict(row))
        for paper_id, row in self._read_materials.items():
            all_materials.setdefault(paper_id, row)

        def nested_supplement_groups(groups: Any):
            pending = list(groups or [])
            while pending:
                group = pending.pop(0)
                if not isinstance(group, Mapping):
                    continue
                pending.extend(group.get("results") or [])
                if group.get("gap_id") or group.get("fulfillment_judgment") or any(
                    group.get(key) for key in ("error", "reason", "usable_content", "useful_material",
                                              "remaining_gap", "still_missing")
                ) or _text(group.get("status")) in {"partial", "failed", "unmet", "unjudgeable"}:
                    yield group

        def chapter_payload(chapter: Mapping[str, Any], index: int) -> tuple[str, dict[str, Any]]:
            chapter_id = _text(chapter.get("chapter_id"))
            source_ids = chapter.get("source_ids") or []
            if isinstance(source_ids, str):
                source_ids = [source_ids]
            source_materials = []
            for source_id in source_ids:
                candidate = candidates.get(str(source_id))
                if candidate is None:
                    material = all_materials.get(str(source_id))
                    if material:
                        source_materials.append({"paper_id": str(source_id), "source_role": "supplement_or_deep_read", "material": material})
                    continue
                material = build_local_material_payload(
                    candidate, deep_material=all_materials.get(str(source_id)),
                )
                material["card_path"] = _text(candidate.get("card_path"))
                # Keep the legacy pool summary fallback for identity-compatible
                # cards only; a mismatched card must not regain its old text.
                if not material.get("review_planning_B") and not material.get("material_identity_conflicts"):
                    planning = candidate.get("planning_view") if isinstance(candidate.get("planning_view"), Mapping) else {}
                    material["review_planning_B"] = {
                        "planning_summary": planning.get("planning_summary"),
                        "facet_contributions": planning.get("facet_contributions"),
                        "scope_interpretation_cautions": planning.get("scope_interpretation_cautions"),
                    }
                source_materials.append(material)
            related_ids = set(str(item) for item in source_ids)
            related_tools = []
            for result in (level1_tools, level2_tools, chapter_tools or {}):
                for group in nested_supplement_groups(result.get("supplement_results")):
                    chapters_for_gap = set(str(item) for item in group.get("chapter_ids") or [])
                    if chapters_for_gap and chapter_id not in chapters_for_gap:
                        continue
                    judgment = group.get("fulfillment_judgment")
                    if isinstance(judgment, Mapping):
                        judgment = {
                            key: judgment[key]
                            for key in (
                                "status", "outcome", "substantive_gap_status", "material_ready",
                                "useful_material", "remaining_gap", "outline_action", "evidence", "limitations",
                            )
                            if key in judgment
                        }
                    related_tools.append({
                        "gap_id": _text(group.get("gap_id")),
                        "chapter_ids": list(group.get("chapter_ids") or []),
                        "status": _text(group.get("status")),
                        "substantive_gap_status": _text(group.get("substantive_gap_status")),
                        "outcome": _text(group.get("outcome")),
                        "fulfillment_judgment": judgment if isinstance(judgment, Mapping) else {},
                        **{key: group[key] for key in ("gap_question", "question", "error", "reason",
                            "usable_content", "useful_material", "auxiliary_material", "remaining_gap",
                            "still_missing", "limitations", "outline_action", "output_dir", "derived_pool_path")
                           if key in group},
                    })
                for group in result.get("directed_results") or []:
                    if not isinstance(group, Mapping):
                        continue
                    for material in group.get("materials") or []:
                        if isinstance(material, Mapping) and _text(material.get("paper_id")) in related_ids:
                            related_tools.append({"paper_id": _text(material.get("paper_id")), "material": dict(material)})
            payload = {
                "topic_id": self.config.topic_id,
                "research_question": topic,
                "shared_outline": shared_outline,
                "chapter": dict(chapter),
                "position": {"index": index + 1, "count": len(chapters)},
                "next_chapter_title": _text(chapters[index + 1].get("title")) if index + 1 < len(chapters) else "",
                "previous_chapter_title": _text(chapters[index - 1].get("title")) if index > 0 else "",
                "source_materials": source_materials,
                "relevant_tool_feedback": related_tools,
                "citation_rules": dict(CURRENT_CITATION_RULES),
            }
            if self.config.planning_revision_enabled:
                navigation = build_candidate_navigation(
                    chapter=chapter,
                    candidates=candidate_pool or candidates,
                    source_routes=routing_rows,
                    research_question=topic,
                    index_path=self.config.local_material_index_path,
                    candidate_limit=self.config.planning_revision_candidate_limit,
                    passages_per_paper=self.config.planning_revision_passages_per_paper,
                    passage_chars=self.config.planning_revision_passage_chars,
                    deep_material_by_paper=all_materials,
                )
                payload["candidate_navigation"] = navigation
                payload["candidate_materials"] = list(navigation.get("candidate_materials") or [])
            return chapter_id, payload

        def build_one(item: tuple[int, Mapping[str, Any]]) -> tuple[int, dict[str, Any]]:
            index, chapter = item
            chapter_id, payload = chapter_payload(chapter, index)
            cached = root / (_safe_id(chapter_id) + ".json")
            def projected_source(item: Mapping[str, Any]) -> dict[str, Any]:
                route = route_by_handle.get(item.get("source_handle"))
                if self.config.planning_revision_enabled:
                    # The chapter owner's research understanding comes from the
                    # real A/B material of its assigned sources.  The routing
                    # note stays available as separate selection advice and
                    # never replaces the source's own account.
                    row = {
                        "source_handle": item.get("source_handle"),
                        "title": item.get("title"),
                        "material_depth": item.get("material_depth"),
                        "study_summary_A": item.get("study_summary_A") or {},
                        "planning_material": item.get("review_planning_B"),
                        "supplement_material": item.get("supplement_gap_material") or {},
                        "supplement_materials": [
                            dict(supplement) for supplement in (item.get("supplement_gap_materials") or [])
                            if isinstance(supplement, Mapping)
                        ],
                        "deep_read_material": _compact_reading_material(item.get("deep_read_material") or {}),
                    }
                    if isinstance(route, Mapping) and route:
                        row["routing_note"] = {
                            key: route[key]
                            for key in ("chapter_ids", "specific_usable_material", "interpretation_limits", "reason")
                            if key in route
                        }
                    return row
                return {
                    "source_handle": item.get("source_handle"), "title": item.get("title"),
                    "material_depth": item.get("material_depth"),
                    "planning_material": route or item.get("review_planning_B"),
                    "supplement_material": item.get("supplement_gap_material") or {},
                    "deep_read_material": _compact_reading_material(item.get("deep_read_material") or {}),
                }

            model_payload = {**payload, "source_materials": [
                projected_source(item)
                for item in payload["source_materials"]
            ], "relevant_tool_feedback": [item for item in payload["relevant_tool_feedback"] if not item.get("material")]}
            model_payload["chapter"] = {key: value for key, value in chapter.items()
                                        if key not in {"source_ids", "paper_ids", "source_handles"}}
            if self.config.planning_revision_enabled:
                model_payload["candidate_navigation"] = payload.get("candidate_navigation") or {}
                model_payload["candidate_materials"] = payload.get("candidate_materials") or []
                model_payload["planning_revision_mode"] = True
                # An unanswered retrieval need bounds what this chapter may
                # currently conclude; it is never evidence that the field
                # lacks the research.  Stated next to the tool feedback it
                # qualifies, so the owner reads it as scope, not as absence.
                model_payload["tool_feedback_scope"] = {
                    "unanswered_need_means": "当前池未取得该内容，仅约束本次可写结论；不构成领域缺失或不存在更优路线的证据",
                }
                model_payload["required_behavior"] = {
                    "inspect_relevant_unassigned_candidates": True,
                    "use_candidate_materials_when_relevant": True,
                    "do_not_treat_local_passages_as_deep_read": True,
                    "do_not_force_use": True,
                }
            # Work order 05: material gathered by the shared retrieval queue for
            # this chapter must be part of the chapter's own input, so the plan
            # can change because of it instead of only gaining a citation.
            chapter_tool_materials = [
                _tool_material_for_prompt(item)
                for item in (tool_materials_by_chapter or {}).get(_text(chapter.get("chapter_id")), [])
            ]
            if chapter_tool_materials:
                model_payload["new_tool_materials"] = chapter_tool_materials
                model_payload["required_behavior"] = {
                    **(model_payload.get("required_behavior") or {}),
                    "use_new_tool_materials": True,
                    "state_what_changed": True,
                    "keep_conditions_and_limits": True,
                    "do_not_only_append_citations": True,
                }
            # Cache the model's assigned/candidate projections separately, not
            # the expanded handoff list (which appends candidate rows below).
            # The contract also covers task/scope, prompt and model settings.
            input_contract = self._cache_contract("chapter_details", model_payload)
            cached_packet = dict(_read_json(cached)) if resume and cached.is_file() else {}
            cached_plan = cached_packet.get("chapter_plan")
            if (cached_packet.get("_chapter_details_input_contract") == input_contract
                    and isinstance(cached_plan, Mapping) and cached_plan.get("units")):
                chapter_plan = dict(cached_plan)
                adaptive_meta = cached_packet.get("chapter_details_adaptive")
                record = {"telemetry": cached_packet.get("planner_telemetry") or {}}
            else:
                batch_cache_root = root / f"{_safe_id(chapter_id)}_details_batches"
                record, adaptive_meta = _chapter_details_adaptive_record(
                    self.planner,
                    model_payload,
                    chapter_id=chapter_id,
                    cache_root=batch_cache_root,
                    resume=resume,
                )
                response = _stage_response(record)
                chapter_plan = response.get("chapter_plan") if isinstance(response.get("chapter_plan"), Mapping) else response
                if adaptive_meta and isinstance(chapter_plan, Mapping):
                    chapter_plan = _chapter_plan_with_arrangement_units(chapter_plan)
            packet_source_materials = [dict(item) for item in payload["source_materials"]]
            existing_handles = {_text(item.get("source_handle")) for item in packet_source_materials}
            for item in payload.get("candidate_materials") or []:
                if not isinstance(item, Mapping):
                    continue
                handle = _text(item.get("source_handle"))
                if handle and handle not in existing_handles:
                    packet_source_materials.append({**dict(item), "source_role": "candidate_navigation"})
                    existing_handles.add(handle)
            packet = {
                "schema_version": SCHEMA_VERSION,
                "topic_id": self.config.topic_id,
                "research_question": topic,
                "shared_outline": shared_outline,
                "chapter": dict(chapter),
                "position": payload["position"],
                "adjacent_chapters": {"previous": payload["previous_chapter_title"], "next": payload["next_chapter_title"]},
                "chapter_plan": dict(chapter_plan),
                "source_materials": packet_source_materials,
                "source_identity_map": {
                    _text(item.get("source_handle")): {
                        "paper_id": _text(item.get("paper_id")),
                        "title": _text(item.get("title")),
                        "doi": _text(item.get("doi")),
                        "year": _text(item.get("year")),
                    }
                    for item in packet_source_materials
                    if _text(item.get("source_handle"))
                },
                "relevant_tool_feedback": payload["relevant_tool_feedback"],
                **({
                    "candidate_navigation": payload.get("candidate_navigation") or {},
                    "candidate_materials": payload.get("candidate_materials") or [],
                } if self.config.planning_revision_enabled else {}),
                "citation_namespace_rule": dict(CURRENT_CITATION_RULES),
                "writer_handoff": {
                    "research_already_done": True,
                    "do_not_research_again": True,
                    "cite_only_supplied_source_handles": True,
                    "preserve_study_types_conditions_and_limitations": True,
                    "write_connected_review_prose_not_question_answer_blocks": True,
                },
                "planner_telemetry": record.get("telemetry") or {},
                "_adaptive_input_materials": chapter_tool_materials,
                "_chapter_details_input_contract": input_contract,
                **({"chapter_details_adaptive": adaptive_meta} if adaptive_meta else {}),
            }
            _atomic_json(cached, packet)
            md = render_writer_packet_markdown(packet)
            (cached.with_suffix(".md")).write_text(md, encoding="utf-8", newline="\n")
            return index, packet

        indexed: dict[int, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=max(1, self.config.chapter_workers)) as executor:
            futures = [executor.submit(build_one, (index, chapter)) for index, chapter in enumerate(chapters)]
            for future in as_completed(futures):
                index, packet = future.result()
                indexed[index] = packet
        records = [indexed[index] for index in sorted(indexed)]
        state["completed_chapters"] = [str(packet.get("chapter", {}).get("chapter_id")) for packet in records]
        _atomic_json(self.config.output_dir / "RUN_STATE.json", state)
        return records

    @staticmethod
    def _partial_result(topic: str, plan: Mapping[str, Any], provisional: Mapping[str, Any], level1_outline: Mapping[str, Any], level1_tools: Mapping[str, Any], level: str, *, harmonized: Mapping[str, Any] | None = None, level2_tools: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "partial",
            "completed_through": level,
            "research_question": topic,
            "review_title": _text(provisional.get("review_title") or topic),
            "original_plan": dict(plan),
            "provisional_scope": dict(provisional),
            **_review_guidance(level1_outline, harmonized or {}),
            "material_theme_inventory": provisional.get("material_theme_inventory") or [],
            "shared_outline": (harmonized or {}).get("shared_outline") or level1_outline.get("shared_outline") or {},
            "level1_tool_results": level1_tools,
            "level2_tool_results": dict(level2_tools or {}),
        }

    @staticmethod
    def _source_index(chapters: Sequence[Mapping[str, Any]], pool_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        assigned: dict[str, list[str]] = {}
        for chapter in chapters:
            for paper_id in chapter.get("source_ids") or []:
                assigned.setdefault(str(paper_id), []).append(_text(chapter.get("chapter_id")))
        b_by_id = {str(row.get("_paper_id")): row.get("_b_summary") or {} for row in pool_rows}
        return {
            paper_id: {
                "paper_id": paper_id,
                "title": _text((b_by_id.get(paper_id) or {}).get("title")),
                "chapter_ids": list(dict.fromkeys(chapter_ids)),
                "selected": bool(chapter_ids),
                "selection_status": "assigned_to_chapter" if chapter_ids else "screened_not_selected",
            }
            for paper_id, chapter_ids in {**{key: assigned.get(key, []) for key in b_by_id}, **{key: value for key, value in assigned.items() if key not in b_by_id}}.items()
        }

    @staticmethod
    def _source_identity_map(pool_rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        """Keep the compact planner handles connected to canonical identities."""
        output: dict[str, dict[str, Any]] = {}
        for row in pool_rows:
            handle = _text(row.get("_source_handle"))
            paper_id = _text(row.get("_paper_id") or _canonical_paper_id(row))
            if not handle or not paper_id:
                continue
            planning = row.get("planning_view") if isinstance(row.get("planning_view"), Mapping) else {}
            identity = planning.get("paper_identity") if isinstance(planning.get("paper_identity"), Mapping) else {}
            output[handle] = {
                "source_handle": handle,
                "paper_id": paper_id,
                "title": _text(identity.get("title") or row.get("title")),
                "doi": _text(identity.get("doi") or row.get("doi")),
                "year": _text(identity.get("year") or row.get("year")),
                "card_path": _text(row.get("card_path")),
            }
        return output

    @staticmethod
    def _improvement_entries(improvement: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Normalize the several model response shapes used for chapter edits."""
        entries: list[dict[str, Any]] = []
        for key in ("updated_chapter_plans", "chapter_updates", "affected_chapters", "cross_chapter_adjustments", "chapter_feedback"):
            raw = improvement.get(key)
            if isinstance(raw, Mapping):
                raw = [
                    ({**dict(value), "chapter_id": key_id} if isinstance(value, Mapping) else {"chapter_id": key_id, "adjustment": value})
                    for key_id, value in raw.items()
                ]
            if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
                continue
            for item in raw:
                if isinstance(item, Mapping):
                    entries.append(dict(item))
        return entries

    @staticmethod
    def _improvement_plan(item: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Return a chapter plan payload, including direct plan-shaped entries."""
        for key in ("chapter_plan", "updated_plan", "updated_chapter_plan", "changes"):
            plan = item.get(key)
            if isinstance(plan, Mapping):
                return plan
        if any(key in item for key in ("units", "substantive_units", "ordered_substantive_units")):
            return item
        return None

    @staticmethod
    def _contains_unit_structure(value: Mapping[str, Any]) -> bool:
        """Unit lists require a complete chapter rewrite, never a shallow patch."""
        if value.get("_complete_chapter_revision") is True:
            return False
        if any(key in value for key in ("units", "substantive_units", "ordered_substantive_units")):
            return True
        target_paths = value.get("target_paths") or []
        if isinstance(target_paths, str):
            target_paths = [target_paths]
        if any(
            ".units" in _text(path)
            or "units[" in _text(path)
            or _text(path).endswith("units")
            for path in target_paths
        ):
            return True
        for key in ("chapter_plan", "updated_plan", "updated_chapter_plan", "changes"):
            nested = value.get(key)
            if isinstance(nested, Mapping) and ProgressiveReviewPlanner._contains_unit_structure(nested):
                return True
        return False

    @staticmethod
    def _editorial_feedback_entries(feedback: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        """Normalize chapter-scoped human/editorial corrections for revision."""
        if not isinstance(feedback, Mapping):
            return []
        entries: list[dict[str, Any]] = []

        def add(raw: Any, chapter_hint: str = "") -> None:
            if isinstance(raw, Mapping):
                item = dict(raw)
                chapter_id = _text(item.get("chapter_id") or item.get("id") or chapter_hint)
                if chapter_id:
                    item["chapter_id"] = chapter_id
                    entries.append(item)
                return
            if isinstance(raw, str) and chapter_hint:
                entries.append({"chapter_id": chapter_hint, "feedback": raw})
                return
            if isinstance(raw, list):
                for item in raw:
                    add(item, chapter_hint)

        for key in ("edits", "chapter_updates", "updated_chapter_plans"):
            raw = feedback.get(key)
            if isinstance(raw, Mapping):
                for chapter_id, value in raw.items():
                    add(value, _text(chapter_id))
            else:
                add(raw)
        for key in ("chapters", "chapter_feedback"):
            raw = feedback.get(key)
            if isinstance(raw, Mapping):
                for chapter_id, value in raw.items():
                    if isinstance(value, list):
                        for item in value:
                            add(item, _text(chapter_id))
                    else:
                        add(value, _text(chapter_id))
            else:
                add(raw)
        if feedback.get("chapter_id"):
            add(feedback)
        seen: set[str] = set()
        output: list[dict[str, Any]] = []
        for item in entries:
            encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, default=_json_default)
            if encoded not in seen:
                output.append(item)
                seen.add(encoded)
        return output

    @classmethod
    def _affected_chapter_ids(cls, improvement: Mapping[str, Any]) -> list[str]:
        ids: list[str] = []
        for key in ("affected_chapter_ids", "chapter_ids"):
            values = improvement.get(key) or []
            if isinstance(values, str):
                values = [values]
            if isinstance(values, Sequence):
                ids.extend(_text(value) for value in values if _text(value))
        for item in cls._improvement_entries(improvement):
            chapter_id = _text(item.get("chapter_id") or item.get("id"))
            if chapter_id:
                ids.append(chapter_id)
            nested = item.get("chapter_ids") or item.get("affected_chapter_ids") or []
            if isinstance(nested, str):
                nested = [nested]
            if isinstance(nested, Sequence):
                ids.extend(_text(value) for value in nested if _text(value))
        return list(dict.fromkeys(ids))

    @classmethod
    def _concrete_improvement_ids(cls, improvement: Mapping[str, Any]) -> set[str]:
        concrete: set[str] = set()
        requires_revision: set[str] = set()
        for item in cls._improvement_entries(improvement):
            chapter_id = _text(item.get("chapter_id") or item.get("id"))
            plan = cls._improvement_plan(item)
            if not chapter_id:
                continue
            if cls._contains_unit_structure(item):
                requires_revision.add(chapter_id)
            elif isinstance(plan, Mapping) and plan:
                concrete.add(chapter_id)
        return concrete - requires_revision

    @classmethod
    def _apply_improvements(cls, chapters: Sequence[Mapping[str, Any]], improvement: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Apply only concrete whole-plan edits targeted to a known chapter."""
        edits: dict[str, tuple[Mapping[str, Any], bool]] = {}
        for item in cls._improvement_entries(improvement):
            chapter_id = _text(item.get("chapter_id"))
            patch = item.get("changes")
            plan = cls._improvement_plan(item)
            # A unit list is a complete chapter structure.  The corresponding
            # entry remains feedback for affected_chapter_revision and must not
            # replace the existing units during this light pass.
            if chapter_id and isinstance(plan, Mapping) and not cls._contains_unit_structure(item):
                edits[chapter_id] = (plan, isinstance(patch, Mapping) and plan is patch)
        output: list[dict[str, Any]] = []
        for row in chapters:
            packet = dict(row)
            chapter = packet.get("chapter") if isinstance(packet.get("chapter"), Mapping) else {}
            chapter_id = _text(chapter.get("chapter_id"))
            if chapter_id in edits:
                plan, is_patch = edits[chapter_id]
                existing = packet.get("chapter_plan") if isinstance(packet.get("chapter_plan"), Mapping) else {}
                packet["chapter_plan"] = {**dict(existing), **dict(plan)}
                packet["whole_plan_edit_applied"] = True
            output.append(packet)
        return output

    def _bind_tool_material_source_handles(self, pool_rows: Sequence[Mapping[str, Any]]) -> None:
        """Resolve supplement identities to program-managed pool handles."""
        by_paper = {_text(row.get("_paper_id")): _text(row.get("_source_handle"))
                    for row in pool_rows if _text(row.get("_paper_id")) and _text(row.get("_source_handle"))}
        by_unit = {_text(row.get("supplement_source_unit_id")): row
                   for row in pool_rows if _text(row.get("supplement_source_unit_id")) and _text(row.get("_source_handle"))}
        for rows in self.tool_materials_by_chapter.values():
            for material in rows:
                if not isinstance(material, Mapping):
                    continue
                sources = material.get("sources")
                if not isinstance(sources, list):
                    continue
                for source in sources:
                    if not isinstance(source, dict):
                        continue
                    identity = source.get("record_identity") if isinstance(source.get("record_identity"), Mapping) else {}
                    for key in ("paper_id", "canonical_paper_id", "title", "doi", "year"):
                        if not source.get(key) and identity.get(key):
                            source[key] = identity[key]
                    current = _text(source.get("source_handle"))
                    paper_id = _text(source.get("paper_id") or source.get("canonical_paper_id"))
                    unit_id = _text(source.get("source_unit_id") or source.get("supplement_source_unit_id"))
                    unit = by_unit.get(unit_id) or {}
                    # Unit provenance cannot override a contradictory stable ID.
                    unit_handle = (_text(unit.get("_source_handle"))
                                   if not paper_id or paper_id == _text(unit.get("_paper_id")) else "")
                    # Unknown historical P labels remain unsafe even before the
                    # current pool has allocated that label. Preserve the source
                    # under its stable identity until normal registration.
                    source["source_handle"] = by_paper.get(paper_id) or unit_handle or paper_id
                    if unit_handle and not paper_id:
                        source["paper_id"] = _text(unit.get("_paper_id"))
                    if not source["source_handle"] and current:
                        # Membership alone cannot establish which run minted a
                        # handle-only source. Keep the unresolved label visible.
                        source["unresolved_source_handle"] = current

    def _assemble_final(self, *, topic: str, plan: Mapping[str, Any], pool_rows: Sequence[Mapping[str, Any]], provisional: Mapping[str, Any], level1_outline: Mapping[str, Any], harmonized: Mapping[str, Any], chapters: Sequence[Mapping[str, Any]], chapter_records: Sequence[Mapping[str, Any]], level1_tools: Mapping[str, Any], level2_tools: Mapping[str, Any], improvement: Mapping[str, Any], chapter_tools: Mapping[str, Any] | None = None) -> dict[str, Any]:
        # Writer handoff follows the final chapter plans, not superseded proposal text.
        chapter_records = json.loads(json.dumps(chapter_records, ensure_ascii=False, default=_json_default))
        all_tool_materials = [
            item for rows in self.tool_materials_by_chapter.values()
            for item in rows if isinstance(item, Mapping)
        ]
        if all_tool_materials:
            chapter_records = merge_tool_materials_into_packets(chapter_records, all_tool_materials)
        identity_map = self._source_identity_map(pool_rows)
        for packet in chapter_records:
            packet_map = dict(packet.get("source_identity_map") or {})
            for source in [*(packet.get("source_materials") or []),
                           *[source for item in packet.get("tool_materials") or []
                             for source in (item.get("sources") or []) if isinstance(source, Mapping)]]:
                if not isinstance(source, Mapping):
                    continue
                handle = _text(source.get("source_handle"))
                if not handle:
                    continue
                packet_map.setdefault(handle, {
                    "source_handle": handle,
                    "paper_id": _text(source.get("paper_id")),
                    "title": _text(source.get("title")),
                    "doi": _text(source.get("doi")),
                    "year": _text(source.get("year")),
                })
                if handle in identity_map:
                    packet_map[handle] = dict(identity_map[handle])
            packet["source_identity_map"] = packet_map
        shared_outline = []
        for packet in chapter_records:
            packet.pop("_adaptive_input_materials", None)
            detail = packet.get("chapter_plan") or {}
            chapter = packet["chapter"]
            chapter["title"] = detail.get("title") or chapter.get("title")
            chapter["purpose"] = detail.get("reader_objective") or chapter.get("purpose")
            chapter["scope"] = detail.get("thesis") or chapter.get("scope")
            chapter["substantive_threads"] = [unit.get("substantive_point") or unit.get("point") for unit in _chapter_units(detail) if isinstance(unit, Mapping)]
            shared_outline.append({key: chapter.get(key) for key in ("chapter_id", "title", "purpose", "scope")})
        for packet in chapter_records:
            packet["shared_outline"] = shared_outline
        source_index = self._source_index(chapters, pool_rows)
        unique_selected = {paper_id for row in source_index.values() if row.get("selected") for paper_id in [row.get("paper_id")]}
        all_deep_ids = list(dict.fromkeys([*_all_directed_ids(level1_tools), *_all_directed_ids(level2_tools), *_all_directed_ids(chapter_tools or {})]))
        guidance = _review_guidance(level1_outline, harmonized, improvement)
        material_themes = improvement.get("material_theme_inventory") or provisional.get("material_theme_inventory") or []
        finalized_notes = improvement.get("finalized_harmonization_notes")
        if finalized_notes is None:
            finalized_notes = improvement.get("harmonization_notes")
        if finalized_notes is None:
            finalized_notes = harmonized.get("harmonization_notes") or []
        if isinstance(finalized_notes, (str, bytes)):
            finalized_notes = [str(finalized_notes)]
        elif not isinstance(finalized_notes, list):
            finalized_notes = list(finalized_notes or []) if isinstance(finalized_notes, Sequence) else [finalized_notes]
        for packet in chapter_records:
            # The current program contract is authoritative even when an old
            # cached packet carries stale editorial instructions.
            packet["citation_namespace_rule"] = dict(CURRENT_CITATION_RULES)
            packet.update(guidance)
        final = {
            "schema_version": SCHEMA_VERSION,
            "status": "complete",
            "topic_id": self.config.topic_id,
            "research_question": topic,
            "review_title": _text(provisional.get("review_title") or harmonized.get("review_title") or plan.get("review_title") or topic),
            "original_plan": dict(plan),
            "provisional_scope": dict(provisional),
            **guidance,
            "material_theme_inventory": material_themes,
            "shared_outline": shared_outline,
            "harmonization_notes": finalized_notes,
            "citation_rules": dict(CURRENT_CITATION_RULES),
            "chapters": [dict(row) for row in chapter_records],
            "candidate_screening": {
                "pool_rows_read": len(pool_rows),
                "unique_sources_assigned": len(unique_selected),
                "candidate_rows": list(source_index.values()),
                "reference_count_is_not_a_gate": True,
            },
            "source_identity_map": self._source_identity_map(pool_rows),
            "planning_tool_results": {"level1": level1_tools, "level2": level2_tools, "chapters": dict(chapter_tools or {})},
            "deep_read_budget": {
                "configured_unique_paper_limit": self.config.shared_deep_read_budget,
                "unique_papers_consumed": len(set(all_deep_ids)),
                "paper_ids": all_deep_ids,
            },
            "whole_plan_improvement": dict(improvement),
            "writer_packets": [
                {
                    "chapter_id": _text(row.get("chapter", {}).get("chapter_id")),
                    "json_path": f"writer_packets/{_safe_id(row.get('chapter', {}).get('chapter_id'))}.json",
                    "markdown_path": f"writer_packets/{_safe_id(row.get('chapter', {}).get('chapter_id'))}.md",
                    "source_material_count": len(row.get("source_materials") or []),
                }
                for row in chapter_records
            ],
        }
        unresolved_owner = [
            _text(item) for item in (improvement.get("owner_revision_unresolved") or []) if _text(item)
        ]
        if unresolved_owner:
            final["status"] = "partial"
            final["owner_revision_unresolved"] = list(dict.fromkeys(unresolved_owner))
        return final

    def _write_final_outputs(self, plan_output: Mapping[str, Any]) -> None:
        root = self.config.output_dir
        packets = plan_output.get("chapters") or []
        packet_root = root / "writer_packets"
        packet_root.mkdir(parents=True, exist_ok=True)
        for packet in packets:
            packet["chapter_plan"] = _supply_local_citation_identity(packet.get("chapter_plan") or {}, packet.get("source_identity_map") or {})
            chapter_id = _safe_id((packet.get("chapter") or {}).get("chapter_id"))
            _atomic_json(packet_root / f"{chapter_id}.json", packet)
            (packet_root / f"{chapter_id}.md").write_text(render_writer_packet_markdown(packet), encoding="utf-8", newline="\n")
        _atomic_json(root / "DETAILED_REVIEW_PLAN.json", plan_output)
        (root / "DETAILED_REVIEW_PLAN.md").write_text(render_plan_markdown(plan_output), encoding="utf-8", newline="\n")


def _chapter_units(plan: Mapping[str, Any]) -> list[Any]:
    return next((plan[key] for key in ("units", "substantive_units", "ordered_substantive_units") if isinstance(plan.get(key), list)), [])


def _chapter_plan_with_arrangement_units(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the adaptive packet consumable by arrangement without editing content."""

    output = json.loads(json.dumps(dict(plan), ensure_ascii=False, default=_json_default))
    if not isinstance(output.get("units"), list) and isinstance(output.get("substantive_units"), list):
        output["units"] = list(output["substantive_units"])
    return output


def _unit_study_records(value: Any) -> list[dict[str, Any]]:
    """Read study entries across the planner's different case field names.

    Pending case-layer suggestions (``case_suggestions``) are proposals, not
    established studies, so they are excluded from study records; the owner
    review decides which of them become concrete cases.
    """
    studies: list[dict[str, Any]] = []
    if isinstance(value, Mapping):
        if value.get("source_handle") or value.get("paper_id"):
            studies.append(dict(value))
        else:
            for key, child in value.items():
                if key == "case_suggestions":
                    continue
                studies.extend(_unit_study_records(child))
    elif isinstance(value, list):
        for child in value:
            studies.extend(_unit_study_records(child))
    elif isinstance(value, str) and re.fullmatch(r"P\d{4,}", value.strip()):
        studies.append({"source_handle": value.strip()})
    return studies


def _case_unit_catalog(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for record in records:
        chapter = record.get("chapter") if isinstance(record.get("chapter"), Mapping) else {}
        chapter_id = _text(chapter.get("chapter_id"))
        units = _chapter_units(record.get("chapter_plan") or {})
        for index, unit in enumerate(units):
            if not isinstance(unit, Mapping):
                unit = {"point": _text(unit)}
            compact = {
                key: unit[key] for key in (
                    "unit_id", "title", "point", "substantive_point", "question", "evidence_need",
                    "comparison", "conditions", "limits", "source_handles", "cases", "supporting_studies", "paragraph_briefs",
                ) if key in unit
            }
            compact.pop("supporting_studies", None)
            compact["cases"] = _unit_study_records(unit)
            compact["source_handles"] = list(dict.fromkeys([
                *(unit.get("source_handles") or []),
                *[_text(case.get("source_handle")) for case in compact["cases"] if case.get("source_handle")],
            ]))
            catalog.append({"unit_key": f"{chapter_id}:{index + 1}", "chapter_id": chapter_id,
                            "chapter_title": _text(chapter.get("title")), "unit": compact})
    return catalog


_CASE_MATERIAL_STRING_LIMIT = 1200


def _clip_case_material_strings(value: Any, limit: int = _CASE_MATERIAL_STRING_LIMIT) -> Any:
    """Bound each free-text string in a case-selection material row."""

    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, list):
        return [_clip_case_material_strings(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: _clip_case_material_strings(item, limit) for key, item in value.items()}
    return value


# Bump when the case_groups prompt or output contract changes, so a cached
# batch answered under an older contract is not reused silently.
CASE_GROUPS_PROMPT_CONTRACT = "case_groups.review_v2_04_body_append_contribution"


def _case_unit_task_signature(
    batch_unit_rows: Sequence[Mapping[str, Any]], *, context: Mapping[str, Any] | None = None,
) -> str:
    """Content signature of the unit tasks a case batch actually consumes.

    Covers points, briefs, cases and every other unit field the payload
    sends — a task edit under the same source list must invalidate a cached
    batch answer.
    """

    return hashlib.sha256(json.dumps(
        {"units": list(batch_unit_rows), "context": dict(context or {})},
        ensure_ascii=False, sort_keys=True, default=_json_default
    ).encode("utf-8")).hexdigest()[:16]


def _case_selection_material_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Compact one source row for the case selection model."""

    compact = {
        key: _clip_case_material_strings(row.get(key))
        for key in (
            "source_handle", "paper_id", "title", "doi", "year", "material_depth",
            "study_summary_A", "review_planning_B", "supplement_gap_material",
            "supplement_gap_materials", "supplement_material", "supplement_materials",
            "tool_supplement_materials", "tool_materials", "usable_content", "material", "local_passages",
        )
        if row.get(key) not in (None, "", [], {})
    }
    if row.get("deep_read_material"):
        compact["deep_read_material"] = _clip_case_material_strings(
            _compact_reading_material(row.get("deep_read_material")))
    compact["material_available"] = _owner_material_has_content(row)
    return compact


def _case_material_rows(
    handles: Sequence[str],
    records: Sequence[Mapping[str, Any]],
    pool_rows: Sequence[Mapping[str, Any]],
    read_materials: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve existing, unclipped case material for selection and attachment.

    The case layer used to see only handles and thin routing notes, which let
    it invent experiments for papers it had never read.  Selection support now
    reuses the same material the chapter owner sees: packet source rows first,
    then bounded local card material from the pool.  Unresolved handles stay
    as explicitly unavailable candidates — nothing is fetched here.
    """

    rows_by_handle: dict[str, Mapping[str, Any]] = {}
    for record in records:
        for source in record.get("source_materials") or ():
            if isinstance(source, Mapping):
                handle = _text(source.get("source_handle"))
                if handle and handle not in rows_by_handle:
                    rows_by_handle[handle] = source
    candidate_by_handle = {
        _text(row.get("_source_handle")): row
        for row in pool_rows
        if isinstance(row, Mapping) and _text(row.get("_source_handle"))
    }
    output: list[dict[str, Any]] = []
    for raw in handles:
        handle = _text(raw)
        if not handle:
            continue
        row = rows_by_handle.get(handle)
        if row is None or not _owner_material_has_content(row):
            candidate = candidate_by_handle.get(handle)
            if candidate is not None:
                paper_id = _text(candidate.get("_paper_id") or _canonical_paper_id(candidate))
                current = build_local_material_payload(
                    candidate,
                    deep_material=read_materials.get(paper_id),
                )
                # A thin packet identity must not shadow the same study's
                # actual material. Never graft a different/unknown identity
                # onto this handle, and leave substantive packets untouched.
                same_identity = row is None or any(
                    _text(left) and _text(left).casefold() == _text(right).casefold()
                    for left, right in (
                        (row.get("paper_id") or row.get("canonical_paper_id"), current.get("paper_id")),
                        (row.get("doi"), current.get("doi")),
                    )
                )
                if (row is None or (same_identity and not row.get("material_identity_conflict")
                        and not _owner_identity_conflict(row, current))):
                    row = current
        if row is None:
            output.append({"source_handle": handle, "material_available": False})
            continue
        output.append(dict(row))
    return output


def _case_selection_material_rows(
    handles: Sequence[str],
    records: Sequence[Mapping[str, Any]],
    pool_rows: Sequence[Mapping[str, Any]],
    read_materials: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Bound selection inputs without truncating the formal writer handoff."""

    return [_case_selection_material_row(row) for row in _case_material_rows(
        handles, records, pool_rows, read_materials)]


def _proposed_use_text(study: Mapping[str, Any]) -> str:
    """The selection layer's proposed use, from either field name."""

    return _text(study.get("proposed_use") or study.get("contribution"))


def _existing_study_texts(unit: Mapping[str, Any]) -> set[tuple[str, str]]:
    texts: set[tuple[str, str]] = set()
    for record in _unit_study_records(unit):
        handle = _text(record.get("source_handle"))
        text = _text(
            record.get("contribution")
            or record.get("macro_contribution")
            or record.get("finding")
            or record.get("use")
        )
        if handle and text:
            texts.add((handle, text))
    return texts


def _cross_chapter_source_uses(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Which cited sources serve more than one chapter, and where.

    Given to the whole-plan coordinator so cross-chapter placement is judged
    from actual use, not from citation counting: a handle listed here is a
    fact about the plan, not an instruction to remove it.
    """

    def adopted_content(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: adopted_content(child) for key, child in value.items()
                    if key not in {"case_suggestions", "case_suggestions_reviewed"}}
        if isinstance(value, (list, tuple)):
            return [adopted_content(child) for child in value]
        return value

    chapters_by_handle: dict[str, list[str]] = {}
    for record in records:
        chapter = record.get("chapter") if isinstance(record.get("chapter"), Mapping) else {}
        chapter_id = _text(chapter.get("chapter_id"))
        if not chapter_id:
            continue
        handles, _paper_ids = _source_keys_in_value(adopted_content(record.get("chapter_plan") or {}))
        for handle in sorted(handles):
            owners = chapters_by_handle.setdefault(handle, [])
            if chapter_id not in owners:
                owners.append(chapter_id)
    return [
        {"source_handle": handle, "chapter_ids": owners}
        for handle, owners in sorted(chapters_by_handle.items())
        if len(owners) > 1
    ]


def _archive_reviewed_case_suggestions(
    records: Sequence[Mapping[str, Any]],
    reviewed_chapter_ids: set[str],
) -> list[dict[str, Any]]:
    """Move owner-reviewed case suggestions out of the plan, keep them for audit.

    Chapters whose revision completed have had every pending proposal judged.
    Their ``case_suggestions`` leave the unit (so arrangement and later runs do
    not treat them as pending or as content) and land in the packet-level
    ``case_suggestions_reviewed`` trail.  Chapters whose revision did not
    complete keep their pending suggestions for the next run.
    """

    output: list[dict[str, Any]] = []
    for record in records:
        chapter = record.get("chapter") if isinstance(record.get("chapter"), Mapping) else {}
        chapter_id = _text(chapter.get("chapter_id"))
        if chapter_id not in reviewed_chapter_ids:
            output.append(dict(record))
            continue
        plan = record.get("chapter_plan") if isinstance(record.get("chapter_plan"), Mapping) else {}
        archived = [
            dict(item) for item in (record.get("case_suggestions_reviewed") or [])
            if isinstance(item, Mapping)
        ]
        units = _chapter_units(plan)
        pending: list[tuple[int, list[dict[str, Any]]]] = []
        for index, unit in enumerate(units):
            if isinstance(unit, Mapping) and unit.get("case_suggestions"):
                studies = [dict(item) for item in unit.get("case_suggestions") or []
                           if isinstance(item, Mapping)]
                pending.append((index, studies))
        if not pending:
            output.append(dict(record))
            continue
        plan_copy = json.loads(json.dumps(dict(plan), ensure_ascii=False, default=_json_default))
        copied_units = _chapter_units(plan_copy)
        for index, studies in pending:
            unit = copied_units[index]
            if isinstance(unit, Mapping):
                unit.pop("case_suggestions", None)
        for index, studies in pending:
            archived.append({"unit_key": f"{chapter_id}:{index + 1}", "studies": studies})
        row = dict(record)
        row["chapter_plan"] = plan_copy
        row["case_suggestions_reviewed"] = archived
        output.append(row)
    return output


def _chapter_retrieval_requests(chapters: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract bounded chapter-scoped requests before chapter writing."""
    gaps: list[dict[str, Any]] = []
    directed: list[dict[str, Any]] = []
    for chapter in chapters:
        if not isinstance(chapter, Mapping):
            continue
        chapter_id = _text(chapter.get("chapter_id") or chapter.get("id"))
        if not chapter_id:
            continue
        for raw in chapter.get("supplement_requests") or chapter.get("retrieval_gaps") or ():
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            row["chapter_ids"] = list(dict.fromkeys([chapter_id, *[str(item) for item in row.get("chapter_ids") or () if str(item).strip()]]))
            row.setdefault("gap_id", f"{chapter_id}:gap:{len(gaps) + 1}")
            gaps.append(row)
        for raw in chapter.get("directed_reads") or ():
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            row["chapter_ids"] = list(dict.fromkeys([chapter_id, *[str(item) for item in row.get("chapter_ids") or () if str(item).strip()]]))
            directed.append(row)
        raw_questions = chapter.get("knowledge_gaps") or chapter.get("open_questions") or chapter.get("missing_evidence") or ()
        if isinstance(raw_questions, str):
            raw_questions = [raw_questions]
        for question in raw_questions:
            text = _text(question)
            if not text:
                continue
            gaps.append({
                "gap_id": f"{chapter_id}:question:{len(gaps) + 1}",
                "gap_question": text,
                "chapter_ids": [chapter_id],
                "targeted_queries": [{"query_text": text, "query_type": "question"}],
            })
    return gaps, directed


def _attach_chapter_candidate_sources(
    chapters: Sequence[Mapping[str, Any]],
    pool_rows: Sequence[Mapping[str, Any]],
    tool_result: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Add successful chapter-owned supplement candidates to their chapter only."""
    owners_by_paper: dict[str, set[str]] = {}

    def add_handle(handle: Any, chapter_ids: Sequence[Any]) -> None:
        normalized_handle = _text(handle)
        if not normalized_handle:
            return
        owners = {_text(value) for value in chapter_ids if _text(value)}
        if normalized_handle and owners:
            owners_by_paper.setdefault(normalized_handle, set()).update(owners)

    def add_handles(handles: Any, chapter_ids: Sequence[Any]) -> None:
        if isinstance(handles, (str, bytes)):
            handles = [handles]
        for handle in handles or ():
            if isinstance(handle, Mapping):
                handle = handle.get("source_handle") or handle.get("paper_id") or handle.get("canonical_paper_id")
            add_handle(handle, chapter_ids)

    pending: list[Any] = list(tool_result.get("supplement_results") or [])
    pending.extend(tool_result.get("results") or [])
    while pending:
        group = pending.pop(0)
        if not isinstance(group, Mapping):
            continue
        owners = {_text(value) for value in (group.get("chapter_ids") or []) if _text(value)}
        for raw in group.get("candidate_rows") or []:
            if not isinstance(raw, Mapping):
                continue
            paper_id = _canonical_paper_id(raw)
            if paper_id and owners:
                owners_by_paper.setdefault(paper_id, set()).update(owners)
        pending.extend(group.get("results") or [])

    # Production adaptive retrieval records ownership outside candidate_rows:
    # needs carry owner chapters and new_handles, while owner_content repeats
    # the resolved handles under each chapter.  Read both shapes so a derived
    # pool can add its actual new papers to the owning chapter.
    retrieval_loop = tool_result.get("retrieval_loop") if isinstance(tool_result.get("retrieval_loop"), Mapping) else {}
    for need in retrieval_loop.get("needs") or []:
        if not isinstance(need, Mapping):
            continue
        owners = need.get("owners") or need.get("chapter_ids") or []
        add_handles(need.get("new_handles") or need.get("source_handles") or [], owners)
    owner_content = retrieval_loop.get("owner_content") or {}
    if isinstance(owner_content, Mapping):
        for need_content in owner_content.values():
            if not isinstance(need_content, Mapping):
                continue
            for chapter_id, content in need_content.items():
                if not isinstance(content, Mapping):
                    continue
                add_handles(content.get("handles") or content.get("new_handles") or [], [chapter_id])

    # Some cached loop results expose the same ownership only through the
    # chapter material writer packet.  It is already chapter-scoped, so those
    # handles are safe to associate with that chapter.
    for chapter_id, materials in (tool_result.get("tool_materials_by_chapter") or {}).items():
        for material in materials or []:
            if isinstance(material, Mapping):
                add_handles(
                    material.get("sources") or material.get("source_handles") or [],
                    [chapter_id],
                )
    rows_by_paper = {
        _text(row.get("_paper_id")): row
        for row in pool_rows
        if _text(row.get("_paper_id"))
    }
    rows_by_handle = {
        _text(row.get("_source_handle")): row
        for row in pool_rows
        if _text(row.get("_source_handle"))
    }
    # Normalize production handles and paper IDs into the same key space.
    normalized_owner_map: dict[str, set[str]] = {}
    for raw_key, owners in owners_by_paper.items():
        row = rows_by_paper.get(raw_key) or rows_by_handle.get(raw_key)
        canonical = _text(row.get("_paper_id")) if row is not None else raw_key
        if canonical:
            normalized_owner_map.setdefault(canonical, set()).update(owners)
    owners_by_paper = normalized_owner_map
    output: list[dict[str, Any]] = []
    for raw in chapters:
        chapter = dict(raw)
        chapter_id = _text(chapter.get("chapter_id") or chapter.get("id"))
        if not chapter_id:
            output.append(chapter)
            continue
        existing_ids = chapter.get("source_ids") or chapter.get("paper_ids") or []
        if isinstance(existing_ids, str):
            existing_ids = [existing_ids]
        existing_handles = chapter.get("source_handles") or []
        if isinstance(existing_handles, str):
            existing_handles = [existing_handles]
        for paper_id, owners in owners_by_paper.items():
            if chapter_id not in owners:
                continue
            row = rows_by_paper.get(paper_id)
            if row is None:
                continue
            existing_ids = [*existing_ids, paper_id]
            handle = _text(row.get("_source_handle"))
            if handle:
                existing_handles = [*existing_handles, handle]
        chapter["source_ids"] = list(dict.fromkeys(_text(value) for value in existing_ids if _text(value)))
        chapter["source_handles"] = list(dict.fromkeys(_text(value) for value in existing_handles if _text(value)))
        output.append(chapter)
    return output


def _attach_case_groups(
    records: Sequence[Mapping[str, Any]],
    response: Mapping[str, Any],
    *,
    planning_revision: bool = False,
    body_case_additions: bool = False,
    candidate_rows: Sequence[Mapping[str, Any]] = (),
    read_materials: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Consume the case layer's response into the chapter records.

    Old mode keeps the established contract: accepted studies land directly in
    ``supporting_studies`` with their contribution text.  In planning-revision
    mode the BODY path sets ``body_case_additions=True`` after owner revision,
    restoring the established direct-append behavior.  The contribution is
    retained as writing use and the actual A/B/deep material is attached to the
    packet.  A handle with no material anywhere is not promoted to a case and
    nothing is fabricated.
    """

    result = json.loads(json.dumps(records, ensure_ascii=False, default=_json_default))
    selected_handles = list(dict.fromkeys(
        _text(study.get("source_handle"))
        for addition in response.get("additions") or [] if isinstance(addition, Mapping)
        for study in addition.get("studies") or [] if isinstance(study, Mapping)
        if _text(study.get("source_handle"))
    ))
    # Reuse the same identity-aware material resolution as case selection.
    # The selection projection is clipped; the writer gets the full existing
    # reading, including useful partial or review-reported original material.
    all_sources = {
        _text(source.get("source_handle")): source
        for source in _case_material_rows(selected_handles, result, candidate_rows, read_materials or {})
        if source.get("paper_id") or _owner_material_has_content(source)
    }
    destinations = {f"{record['chapter']['chapter_id']}:{i + 1}": (record, unit)
                    for record in result for i, unit in enumerate(_chapter_units(record.get("chapter_plan") or {})) if isinstance(unit, dict)}
    for addition in response.get("additions") or []:
        destination = destinations.get(addition.get("unit_key"))
        if not destination:
            continue
        record, unit = destination
        if planning_revision and not body_case_additions:
            suggestions = unit.setdefault("case_suggestions", [])
            suggested_seen = {
                (_text(item.get("source_handle")), _text(item.get("proposed_use")))
                for item in suggestions if isinstance(item, Mapping)
            }
            established = _existing_study_texts(unit)
            packet_handles = {item.get("source_handle") for item in record.get("source_materials") or []}
            for study in addition.get("studies") or []:
                if not isinstance(study, Mapping):
                    continue
                handle = _text(study.get("source_handle"))
                proposed_use = _proposed_use_text(study)
                if not handle:
                    continue
                key = (handle, proposed_use)
                if key in suggested_seen or key in established:
                    # An exactly repeated use is not a new use; a distinct
                    # proposal for the same paper still reaches the owner.
                    continue
                source = all_sources.get(handle)
                suggestion = {
                    "source_handle": handle,
                    "proposed_use": proposed_use,
                    "material_available": source is not None and _owner_material_has_content(source),
                }
                if study.get("paper_id"):
                    suggestion["paper_id"] = _text(study.get("paper_id"))
                suggestions.append(suggestion)
                suggested_seen.add(key)
                if source is not None and handle not in packet_handles:
                    record["source_materials"].append(source); packet_handles.add(handle)
                    record.setdefault("source_identity_map", {})[handle] = {key2: source.get(key2) for key2 in ("paper_id", "title", "doi", "year")}
                    record["chapter"].setdefault("source_ids", []).append(source.get("paper_id"))
            continue
        studies = unit.setdefault("supporting_studies", [])
        # A source may already support a paragraph and still have a distinct
        # case use in the same unit.  Deduplicate only against established
        # case fields, rather than treating every paragraph source as an
        # existing case.
        existing_case_fields = {
            key: unit.get(key) or []
            for key in ("supporting_studies", "concrete_studies", "cases_and_sources", "cases_and_references")
        }
        existing = {
            _text(item.get("source_handle"))
            for item in _unit_study_records(existing_case_fields)
            if _text(item.get("source_handle"))
        }
        packet_handles = {item.get("source_handle") for item in record.get("source_materials") or []}
        for study in addition.get("studies") or []:
            if not isinstance(study, Mapping):
                continue
            handle = study.get("source_handle")
            source = all_sources.get(handle)
            current = next((item for item in record.get("source_materials") or []
                            if item.get("source_handle") == handle), None)
            if current is not None:
                if _owner_material_has_content(current):
                    # The same study may carry chapter-specific questions,
                    # conditions or limits. Never replace that context with
                    # another chapter's first source row.
                    source = current
                elif source is not None:
                    resolved, _ = _resolve_owner_source_materials(
                        source_materials=[current], chapter_plan={"source_handles": [handle]},
                        candidate_materials=[source],
                    )
                    source = resolved[0] if resolved else None
            if not source or not _owner_material_has_content(source) or handle in existing:
                continue
            stored_study = dict(study)
            # Old mode consumes contribution through the established
            # arrangement contract. Accept a proposed_use response from a
            # stale/cache-backed caller without dropping its case text.
            if not _text(stored_study.get("contribution")) and _text(stored_study.get("proposed_use")):
                stored_study["contribution"] = _text(stored_study.get("proposed_use"))
            studies.append(stored_study); existing.add(handle)
            if handle in packet_handles:
                record["source_materials"] = [
                    source if item.get("source_handle") == handle else item
                    for item in record["source_materials"]
                ]
            else:
                record.setdefault("source_materials", []).append(source); packet_handles.add(handle)
            record.setdefault("source_identity_map", {})[handle] = {
                key: source.get(key) for key in ("paper_id", "title", "doi", "year")
            }
            source_ids = record["chapter"].setdefault("source_ids", [])
            if source.get("paper_id") and source["paper_id"] not in source_ids:
                source_ids.append(source["paper_id"])
    return result


def _card_for_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    path = Path(str(candidate.get("card_path") or ""))
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _supply_local_citation_identity(value: Any, identity_map: Mapping[str, Any]) -> Any:
    if isinstance(value, list):
        return [_supply_local_citation_identity(item, identity_map) for item in value]
    if not isinstance(value, Mapping):
        return value
    result = {key: _supply_local_citation_identity(item, identity_map) for key, item in value.items()}
    handle = _text(result.get("source_handle") or result.get("reference_handle"))
    if handle and handle not in identity_map:
        handle = next((key for key, row in identity_map.items() if row.get("paper_id") == handle), handle)
    if handle in identity_map:
        if "source_handle" in result:
            result["source_handle"] = handle
        result["paper_id"] = identity_map[handle]["paper_id"]
    return result


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        "".join(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":"), default=_json_default) + "\n" for row in rows),
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def _snapshot_for_candidate(candidate: Mapping[str, Any]) -> Path | None:
    """Find an already-acquired snapshot through card, batch, or source-unit metadata."""
    card_path = Path(str(candidate.get("card_path") or ""))
    if not card_path.is_file():
        return None
    card = _card_for_candidate(candidate)
    material = card.get("material") if isinstance(card.get("material"), Mapping) else {}
    desired_id = _text(material.get("snapshot_id") or card.get("snapshot_id"))
    paper_id = _text(candidate.get("_paper_id") or _canonical_paper_id(candidate))
    for parent in [card_path.parent, *card_path.parents]:
        source_unit_path = parent / "SOURCE_UNIT.json"
        if source_unit_path.is_file():
            try:
                unit = _read_json(source_unit_path)
            except ProgressivePlanError:
                unit = {}
            if isinstance(unit, Mapping):
                snapshot = Path(str(unit.get("snapshot_path") or ""))
                if not snapshot.is_absolute():
                    snapshot = source_unit_path.parent / snapshot
                if snapshot.is_dir() and (not desired_id or _snapshot_id(snapshot) in {"", desired_id}):
                    return snapshot.resolve()
        batch_path = parent / "BATCH_STATE.json"
        if batch_path.is_file():
            try:
                batch = _read_json(batch_path)
            except ProgressivePlanError:
                batch = {}
            items = batch.get("items") if isinstance(batch, Mapping) else {}
            if isinstance(items, Mapping):
                for item in items.values():
                    if not isinstance(item, Mapping):
                        continue
                    if paper_id and _text(item.get("paper_id")) != paper_id:
                        continue
                    snapshot = Path(str(item.get("snapshot_dir") or ""))
                    if not snapshot.is_absolute():
                        snapshot = batch_path.parent / snapshot
                    if not snapshot.is_dir():
                        continue
                    snapshot_id = _snapshot_id(snapshot)
                    if desired_id and snapshot_id and snapshot_id != desired_id:
                        continue
                    return snapshot.resolve()
    return None


def _snapshot_id(snapshot_dir: Path) -> str:
    for name in ("manifest.json", "SNAPSHOT_MANIFEST.json", "snapshot.json"):
        path = snapshot_dir / name
        if not path.is_file():
            continue
        try:
            payload = _read_json(path)
        except ProgressivePlanError:
            continue
        if isinstance(payload, Mapping):
            nested = payload.get("snapshot") if isinstance(payload.get("snapshot"), Mapping) else {}
            return _text(payload.get("snapshot_id") or nested.get("snapshot_id"))
    return ""


def _candidate_identity(candidate: Mapping[str, Any]) -> dict[str, Any]:
    planning = candidate.get("planning_view") if isinstance(candidate.get("planning_view"), Mapping) else {}
    identity = planning.get("paper_identity") if isinstance(planning.get("paper_identity"), Mapping) else {}
    return {**dict(identity), **{key: candidate[key] for key in ("paper_id", "canonical_paper_id", "doi", "title") if candidate.get(key)},
            "paper_id": _text(candidate.get("_paper_id") or _canonical_paper_id(candidate))}


def _directed_source_state(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Current readable content, independent of directories and snapshot names."""
    from .directed_reading import sha256_value
    from .practical_materials import load_practical_material

    identity = _candidate_identity(candidate)
    result = {"paper_identity": identity, "source_hash": ""}
    card = _card_for_candidate(candidate)
    if _saved_card_identity_conflict(identity, card) is not None:
        return {**result, "material_identity_conflict": True}
    snapshot = _snapshot_for_candidate(candidate)
    if snapshot is None:
        return result
    for name in ("manifest.json", "SNAPSHOT_MANIFEST.json", "snapshot.json"):
        path = snapshot / name
        if path.is_file():
            try:
                manifest = _read_json(path)
            except ProgressivePlanError:
                continue
            if isinstance(manifest, Mapping) and _saved_card_identity_conflict(identity, manifest) is not None:
                return {**result, "material_identity_conflict": True}
    try:
        result["source_hash"] = sha256_value(load_practical_material(snapshot))
    except (OSError, UnicodeError, ValueError):
        pass
    return result


def _prior_read_source_hash(material: Mapping[str, Any]) -> str:
    """Recover legacy practical content proof from its already-saved prompt."""
    from .directed_reading import sha256_value

    digest = _text(material.get("source_hash"))
    if digest:
        return digest
    origin = _text(material.get("reused_from"))
    if not origin:
        return ""
    prompt_path = Path(origin).parent / "PROMPT.json"
    if not prompt_path.is_file():
        return ""
    try:
        prompt = _read_json(prompt_path)
        for row in prompt.get("messages") or []:
            if row.get("role") != "user":
                continue
            payload = json.loads(row.get("content") or "{}")
            if isinstance(payload, Mapping) and "source_material" in payload and "optional_bibliography" in payload:
                return sha256_value({"body": payload["source_material"], "references": payload["optional_bibliography"]})
    except (ProgressivePlanError, TypeError, ValueError, AttributeError):
        pass
    return ""


def _prior_read_source_compatible(candidate: Mapping[str, Any], material: Mapping[str, Any]) -> bool:
    identity = _candidate_identity(candidate)
    if _saved_card_identity_conflict(identity, material) is not None:
        return False
    # A review-derived account remains usable without its own snapshot, but
    # its presence alone is not proof that an earlier task answer is unchanged.
    current = _directed_source_state(candidate)
    if current.get("material_identity_conflict"):
        return False
    previous = _prior_read_source_hash(material)
    return bool(previous and current["source_hash"] and previous == current["source_hash"])


def load_prior_readings(paths: Sequence[str | Path], pool_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Load explicitly supplied practical reading outputs that match this pool."""
    pool_by_id = {str(row.get("_paper_id") or _canonical_paper_id(row)): row for row in pool_rows}
    candidates: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            candidates.extend(path.rglob("DIRECTED_READING.json"))
        elif path.is_file():
            candidates.append(path)
    output: dict[str, dict[str, Any]] = {}
    for path in candidates:
        try:
            raw = _read_json(path)
        except ProgressivePlanError:
            continue
        if not isinstance(raw, Mapping):
            continue
        artifact = raw.get("output") if isinstance(raw.get("output"), Mapping) else raw
        identity = artifact.get("paper_identity") if isinstance(artifact.get("paper_identity"), Mapping) else {}
        paper_id = _text(artifact.get("paper_id") or artifact.get("canonical_paper_id") or identity.get("canonical_paper_id"))
        if paper_id not in pool_by_id:
            continue
        pool = pool_by_id[paper_id]
        if _saved_card_identity_conflict(_candidate_identity(pool), artifact) is not None:
            continue
        planning = pool.get("planning_view") if isinstance(pool.get("planning_view"), Mapping) else {}
        pool_identity = planning.get("paper_identity") if isinstance(planning.get("paper_identity"), Mapping) else {}
        expected_title = _text(pool_identity.get("title") or pool.get("title"))
        actual_title = _text(identity.get("title") or artifact.get("paper_title") or artifact.get("title"))
        if expected_title and actual_title and expected_title.casefold() != actual_title.casefold():
            continue
        value = dict(artifact)
        value["paper_id"] = paper_id
        value.setdefault("paper_title", expected_title)
        value["reused_from"] = str(path.resolve())
        source_hash = _prior_read_source_hash(value)
        if source_hash:
            value["source_hash"] = source_hash
        input_path = path.parent / "INPUT.json"
        if not _material_task_signature(value) and input_path.is_file():
            try:
                saved_input = _read_json(input_path)
            except ProgressivePlanError:
                saved_input = {}
            saved_task = saved_input.get("task") if isinstance(saved_input, Mapping) else None
            if (isinstance(saved_task, Mapping) and saved_task.get("questions") and saved_task.get("required_outputs")
                    and _text(saved_input.get("paper_id")) == paper_id
                    and saved_input.get("task_id") == value.get("task_id")):
                value["_progressive_task_signature"] = _directed_task_signature({
                    **dict(saved_task), "knowledge_gaps": saved_task.get("gap_keys") or [],
                })
        output[paper_id] = value
    return list(output.values())


def make_planning_supplement_runner(
    config: ProgressivePlannerConfig,
    *,
    key_file: str | Path,
    budget_ledger_path: str | Path,
    budget_limit_cny: float,
    local_index_path: str | Path | None = None,
    allow_external: bool = True,
) -> Callable[..., Mapping[str, Any]]:
    """Build a callable adapter over the existing practical supplement path.

    Work order 02 makes this local-first: every gap is triaged against the
    already-registered local material before any provider is contacted.  A gap
    whose local material is insufficient keeps its external request for work
    order 03 instead of silently triggering a retrieval here.
    """
    def run(gaps: Sequence[Mapping[str, Any]], **context: Any) -> Mapping[str, Any]:
        if not gaps:
            return {"status": "not_requested", "results": []}
        from .material_acquisition import AcquisitionConfig, MaterialAcquirer
        from .paper_reading_card import run_paper_reading_card
        from .planning_supplement import (
            CompositeS2OpenAlexGateway, QwenCandidateSelector, run_gap_local_triage,
            run_planning_supplement, run_qwen_fulfillment_judge, allocate_supplement_attempt,
        )
        from .planning_material_triage import QwenLocalTriageJudge
        from ...s2_intelligence_gateway import S2IntelligenceGateway
        from tools.academic_backends.openalex_backend import OpenAlexBackend

        phase_root = Path(context.get("output_dir") or config.output_dir / str(context.get("phase") or "tools"))
        inputs_root = phase_root / "tool_inputs"
        base_path = inputs_root / "MERGED_PLANNING_POOL.jsonl"
        input_rows = []
        for row in context.get("pool_rows") or []:
            if isinstance(row, Mapping):
                input_rows.append({key: value for key, value in row.items() if not key.startswith("_")})
        _write_jsonl(base_path, input_rows)
        index_path = Path(local_index_path) if local_index_path else (
            config.output_dir / "planning_material_index.sqlite"
        )
        local_judge = QwenLocalTriageJudge(
            key_file=key_file, budget_ledger_path=budget_ledger_path,
            budget_limit_cny=float(budget_limit_cny), model=config.reader_model,
            timeout_seconds=config.timeout_seconds,
            raw_response_dir=phase_root / "_llm_response_cache",
        ) if Path(key_file).is_file() else None
        user_scope = _text((context.get("plan") or {}).get("research_question") or (context.get("plan") or {}).get("question_en") or (context.get("plan") or {}).get("question")) or _text(config.topic_id)
        local_triage: list[dict[str, Any]] = []
        remaining: list[Mapping[str, Any]] = []
        if context.get("skip_local_triage"):
            remaining = list(gaps)
        else:
            for gap in gaps:
                if not index_path.is_file():
                    remaining.append(gap)
                    continue
                gap_id = _safe_id(gap.get("gap_id") or f"gap_{len(local_triage) + 1:02d}")
                try:
                    triage = run_gap_local_triage(
                        {**dict(gap), "gap_id": gap_id},
                        index_path=index_path,
                        user_scope=user_scope,
                        judge=local_judge,
                        output_dir=phase_root / "local_triage" / gap_id,
                        read_local=True,
                        source_handle_map=context.get("source_handle_map") or {},
                        cache_dir=config.output_dir / "local_lookup",
                        retry_failed=gap.get("retry_empty_result") is True,
                    )
                except Exception as exc:  # local triage failure must not hide the gap
                    local_triage.append({"gap_id": gap_id, "decision": "triage_failed", "error": type(exc).__name__})
                    remaining.append(gap)
                    continue
                local_triage.append(triage)
                if triage.get("decision") == "external_research" and not triage.get("provider_failed"):
                    remaining.append(gap)
        if not remaining:
            return {"status": "local_triage_failed" if any(row.get("provider_failed") for row in local_triage) else "local_material_ready",
                    "results": [], "local_triage": local_triage}
        if not allow_external:
            return {
                "status": "external_research_required",
                "results": [],
                "local_triage": local_triage,
                "pending_gaps": [str(gap.get("gap_id") or "") for gap in remaining],
            }
        judge = run_qwen_fulfillment_judge(
            key_file=key_file,
            budget_ledger_path=budget_ledger_path,
            budget_limit_cny=budget_limit_cny,
        )
        selector = QwenCandidateSelector(
            key_file=key_file,
            budget_ledger_path=budget_ledger_path,
            budget_limit_cny=float(budget_limit_cny),
        ) if Path(key_file).is_file() else None

        def reuse_material(*, record: Mapping[str, Any], pool_rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
            paper_id = _text(record.get("canonical_paper_id") or record.get("paper_id"))
            candidate = next((row for row in pool_rows if _text(row.get("paper_id") or row.get("_paper_id")) == paper_id), None)
            if candidate is None:
                # Search providers may identify a paper with a different
                # provider handle (for example OpenAlex vs CorpusId).  The
                # supplement identity audit already treats a unique DOI as a
                # verified match; use that same stable identity here so an
                # existing local snapshot/card is reused instead of fetched
                # again.  Keep this fallback deliberately narrow: DOI only,
                # and only when there is exactly one pool match.
                from .material_acquisition import normalize_doi

                record_doi = normalize_doi(record.get("doi"))
                if record_doi:
                    doi_matches = []
                    for row in pool_rows:
                        view = row.get("planning_view") if isinstance(row.get("planning_view"), Mapping) else {}
                        identity = view.get("paper_identity") if isinstance(view.get("paper_identity"), Mapping) else {}
                        row_doi = normalize_doi(row.get("doi") or identity.get("doi"))
                        if row_doi == record_doi:
                            doi_matches.append(row)
                    if len(doi_matches) == 1:
                        candidate = doi_matches[0]
            if not isinstance(candidate, Mapping):
                return None
            snapshot = _snapshot_for_candidate(candidate)
            card_path = Path(str(candidate.get("card_path") or ""))
            card = _card_for_candidate(candidate) if card_path.is_file() else {}
            if snapshot is None and not card:
                return None
            return {
                "snapshot": snapshot,
                "card": card,
                "card_path": str(card_path) if card_path.is_file() else "",
                "material_depth": _text((candidate.get("_b_summary") or {}).get("declared_content_depth")) or "fulltext",
            }

        def acquirer_factory(material_root: Path):
            return MaterialAcquirer(material_root, config=AcquisitionConfig(cache_root=material_root / "cache"))

        gateway = CompositeS2OpenAlexGateway(S2IntelligenceGateway(), OpenAlexBackend())
        results: list[dict[str, Any]] = []
        for gap in remaining:
            gap_id = _safe_id(gap.get("gap_id") or f"gap_{len(results) + 1:02d}")
            request_path = inputs_root / gap_id / "REQUEST.json"
            request = {
                "schema_version": "optomind.planning_supplement.request.v1",
                "request_id": f"progressive-{_safe_id(config.topic_id)}-{_safe_id(context.get('phase'))}-{gap_id}",
                "topic_id": config.topic_id,
                "gap_id": gap_id,
                "gap_question": _text(gap.get("gap_question") or gap.get("question")) or "Resolve a scope-relevant literature gap.",
                "success_criteria": list(gap.get("success_criteria") or []) + [
                    _supplement_requirement_text(item) for item in _supplement_requirement_rows(gap.get("required_outputs"))
                    if _supplement_requirement_text(item) not in (gap.get("success_criteria") or [])
                ] or ["Return directly relevant material or clearly state what remains unresolved."],
                "required_outputs": _supplement_requirement_rows(gap.get("required_outputs")),
                "reusable_material": _text(gap.get("reusable_material")),
                "still_missing": _text(gap.get("still_missing")),
                "base_pool_path": str(base_path.resolve()),
                "plan_path": str(config.plan_path.resolve()),
                "targeted_queries": [dict(item) for item in gap.get("targeted_queries") or [] if isinstance(item, Mapping)],
                "reuse_plan_facet_ids": list(gap.get("reuse_plan_facet_ids") or []),
                "known_papers": [dict(item) for item in gap.get("known_papers") or [] if isinstance(item, Mapping)],
                "reviewed_references": [dict(item) for item in gap.get("reviewed_references") or [] if isinstance(item, Mapping)],
                "limits": {"max_candidates": 12, "max_acquisitions": 3, "per_query_limit": 12},
            }
            _atomic_json(request_path, request)
            output_dir = allocate_supplement_attempt(phase_root / "supplements" / gap_id)
            try:
                result = run_planning_supplement(
                    request_path,
                    output_dir=output_dir,
                    gateway=gateway,
                    acquirer_factory=acquirer_factory,
                    card_runner=run_paper_reading_card,
                    fulfillment_judge=judge,
                    card_options={
                        "key_file": str(key_file),
                        "budget_ledger_path": str(budget_ledger_path),
                        "budget_limit_cny": float(budget_limit_cny),
                    },
                    candidate_selector=selector,
                    reuse_material=reuse_material,
                )
                returned_dir = Path(str(result.get("output_dir") or output_dir))
                index_path = returned_dir / "SUPPLEMENT_INDEX.json"
                index = _read_json(index_path) if index_path.is_file() else {}
                results.append({
                    **dict(result),
                    "gap_id": gap_id,
                    "chapter_ids": list(gap.get("chapter_ids") or []),
                    "gap_question": request["gap_question"],
                    "required_outputs": request["required_outputs"],
                    "output_dir": str(returned_dir),
                    "fulfillment_judgment": index.get("fulfillment_judgment") or result.get("fulfillment_judgment") or {},
                    "source_units": index.get("source_units") or (result.get("source_units") if isinstance(result.get("source_units"), list) else []),
                    "citation_anchors": index.get("citation_anchors") if isinstance(index, Mapping) else [],
                    "substantive_gap_status": index.get("substantive_gap_status") or result.get("substantive_gap_status"),
                    "status": result.get("status") or "completed",
                })
            except Exception as exc:
                results.append({"gap_id": gap_id, "chapter_ids": list(gap.get("chapter_ids") or []),
                                "gap_question": request["gap_question"], "required_outputs": request["required_outputs"],
                                "status": "failed", "error": type(exc).__name__, "reason": str(exc),
                                "output_dir": str(output_dir), "still_missing": request["gap_question"]})
        return {
            "status": "completed" if any(row.get("status") not in {"failed", "error"} for row in results) else "failed",
            "results": results,
            "local_triage": local_triage,
            "external_gap_ids": [str(gap.get("gap_id") or "") for gap in remaining],
        }
    return run


def _extend_planning_material_index(index_path: Path, rows: Sequence[Mapping[str, Any]], root: Path, topic_id: str) -> None:
    """Make newly acquired cards/body text available to the very next need."""
    if not rows or not index_path.is_file():
        return
    from .planning_material_search import SearchIndexConfig, PlanningMaterialIndex, build_index
    root.mkdir(parents=True, exist_ok=True)
    pool_path = root / "POOL_DELTA.jsonl"
    pool_path.write_text("\n".join(json.dumps(dict(row), ensure_ascii=False, default=_json_default) for row in rows) + "\n", encoding="utf-8")
    identity = {}
    snapshot_roots = set()
    directed_roots = set()
    with PlanningMaterialIndex(index_path, readonly=True) as old_index:
        existing = {row["paper_id"]: dict(row) for row in old_index.papers()}
    for row in rows:
        paper_id = _text(row.get("_paper_id") or _canonical_paper_id(row))
        handle = _text(row.get("_source_handle"))
        if not paper_id or not handle:
            continue
        compact = row.get("_b_summary") or _compact_b_record(row)
        identity[handle] = {"paper_id": paper_id, "card_path": row.get("card_path"),
                            **{key: compact.get(key) for key in ("title", "doi", "year")}}
        snapshot = _snapshot_for_candidate(row)
        old = existing.get(paper_id) or {}
        if snapshot is None and old.get("snapshot_path"):
            prior_path = Path(old["snapshot_path"])
            snapshot = prior_path if prior_path.is_dir() else None
        if snapshot:
            snapshot_roots.add(snapshot.parent.parent)
        if old.get("directed_reading_path"):
            directed_roots.add(Path(old["directed_reading_path"]).parent.parent)
    if not identity:
        return
    identity_path = root / "IDENTITIES.json"
    _atomic_json(identity_path, {"source_identity_map": identity})
    build_index(SearchIndexConfig(topic_id=topic_id, index_path=index_path, pool_path=pool_path,
                                  identity_map_path=identity_path, snapshot_roots=tuple(snapshot_roots), directed_roots=tuple(directed_roots)))
    with PlanningMaterialIndex(index_path) as index:
        for row in rows:
            materials = row.get("supplement_gap_materials") or [row.get("supplement_gap_material")]
            segments = [{"segment_kind": "directed_content", "section_path": ["Supplementary question reading"],
                         "text": json.dumps(item, ensure_ascii=False), "ordinal": i}
                        for i, item in enumerate(materials) if item]
            if segments:
                index.add_segments(_text(row.get("_paper_id") or _canonical_paper_id(row)), segments)
        index.commit()
        index.record_term_document_frequency()


def _supplement_requirement_rows(value: Any) -> list[Any]:
    if isinstance(value, (str, Mapping)):
        return [value] if value else []
    return list(value or [])


def _supplement_requirement_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return _text(value.get("description") or value.get("requirement") or value.get("output_type")) or json.dumps(
            {key: item for key, item in value.items() if key not in {"output_id", "question_id"}},
            ensure_ascii=False, sort_keys=True,
        )
    return _text(value)


def _supplement_contract_value(value: Any) -> Any:
    """Normalize semantic contract fields, excluding output routing labels."""
    if isinstance(value, Mapping):
        return {key: _supplement_contract_value(item) for key, item in sorted(value.items())
                if key not in {"output_id", "question_id", "required_output_ids"}}
    if isinstance(value, (list, tuple)):
        return sorted({_supplement_contract_token(item) for item in value if item not in (None, "")})
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _supplement_contract_token(value: Any) -> str:
    return json.dumps(_supplement_contract_value(value), ensure_ascii=False, sort_keys=True)


def _supplement_need_contract(request: Mapping[str, Any], need: Any) -> dict[str, Any]:
    research = _supplement_contract_value({
        "question": need.question, "user_scope": need.user_scope, "intended_use": need.intended_use,
        "comparison_object": need.comparison_object, "required_concepts": need.required_concepts,
    })
    acceptance = {key: _supplement_contract_value(request.get(key) or [])
                  for key in ("success_criteria", "required_outputs")}
    encoded = json.dumps({"research": research, "acceptance": acceptance}, ensure_ascii=False, sort_keys=True)
    return {"research": research, "acceptance": acceptance,
            "research_key": hashlib.sha256(json.dumps(research, sort_keys=True).encode()).hexdigest(),
            "reuse_key": hashlib.sha256(encoded.encode()).hexdigest()}


def _supplement_source_fingerprint(row: Mapping[str, Any]) -> str:
    card = _card_for_candidate(row)
    planning = row.get("planning_view") if isinstance(row.get("planning_view"), Mapping) else {}
    # Identity and paths are not scientific changes, and our own supplement
    # notes are additive. Compare the current source A/B actually available.
    material = {
        "A": card.get("general_understanding") or {},
        "B": card.get("review_planning") or {},
        "planning": {key: value for key, value in planning.items() if key != "paper_identity"},
    }
    return _material_content_signature(_owner_cache_projection(material))


def _supplement_material_fingerprints(row: Mapping[str, Any]) -> dict[str, Any]:
    active = row.get("supplement_gap_material")
    materials = [active, *(row.get("supplement_gap_materials") or []), *(row.get("prior_supplement_gap_materials") or [])]
    return {
        "source_unit_id": _text(row.get("supplement_source_unit_id")),
        "active": _material_content_signature(_owner_cache_projection(active)) if isinstance(active, Mapping) and active else "",
        "materials": sorted({_material_content_signature(_owner_cache_projection(item)) for item in materials
                             if isinstance(item, Mapping) and item}),
    }


def _supplement_used_papers(state: Mapping[str, Any]) -> set[str]:
    papers: set[str] = set()
    pending = [state.get("local_triage") or {}, *(state.get("external_results") or [])]
    while pending:
        item = pending.pop()
        if isinstance(item, Mapping):
            identity = item.get("record_identity") if isinstance(item.get("record_identity"), Mapping) else item
            paper_id = _text(identity.get("canonical_paper_id") or identity.get("paper_id"))
            if paper_id:
                papers.add(paper_id)
            for key in ("results", "source_units", "sources", "passages"):
                if isinstance(item.get(key), list):
                    pending.extend(item[key])
            if isinstance(item.get("writer_material"), Mapping):
                pending.append(item["writer_material"])
    return papers


def _supplement_answered(state: Mapping[str, Any]) -> bool:
    return isinstance(state, Mapping) and state.get("status") == "answered" and bool(_text(state.get("usable_content"))) and not _text(state.get("still_missing"))


def _local_writer_context(local: Mapping[str, Any], *, omit_gap_limit: bool = False) -> dict[str, Any]:
    raw = local.get("writer_material") if isinstance(local, Mapping) else None
    writer = dict(raw) if isinstance(raw, Mapping) else {}
    if omit_gap_limit:
        from .planning_material_triage import WRITER_MATERIAL_SCHEMA
        missing = _text(local.get("still_missing"))
        if missing and writer.get("schema_version") == WRITER_MATERIAL_SCHEMA:
            # build_writer_material mirrors its task gap in limits. That is
            # not a source limitation; preserve every other limitation.
            writer["limits"] = [item for item in writer.get("limits") or [] if item != missing]
    return writer


def _supplement_bind_results(results: Sequence[Mapping[str, Any]], owners: Sequence[str]) -> list[dict[str, Any]]:
    output = []
    for raw in results:
        row = dict(raw)
        row["chapter_ids"] = list(owners)
        if isinstance(row.get("results"), list):
            row["results"] = _supplement_bind_results(row["results"], owners)
        output.append(row)
    return output


def make_retrieval_loop_runner(
    config: ProgressivePlannerConfig,
    *,
    key_file: str | Path | None = None,
    budget_ledger_path: str | Path | None = None,
    budget_limit_cny: float | None = None,
    local_index_path: str | Path | None = None,
    allow_external: bool = True,
    supplement_runner: Callable[..., Mapping[str, Any]] | None = None,
    directed_reader: Callable[..., Mapping[str, Any]] | None = None,
    prior_readings: Sequence[Mapping[str, Any]] = (),
) -> Callable[..., Mapping[str, Any]]:
    """Adapt planner requests to the shared bounded retrieval queue.

    The queue owns the local-first decision and round bounds.  The existing
    supplement and directed-reading adapters remain the only provider-facing
    closures, which keeps the 581-card pool and the cumulative deep-read
    ledger intact while allowing the same narrow request at every level.
    """

    read_cache_path = config.output_dir / "DIRECTED_MATERIAL_CACHE.json"
    read_cache = _read_json(read_cache_path) if read_cache_path.is_file() else {}
    read_materials = dict(read_cache.get("materials") or {})
    read_task_signatures = dict(read_cache.get("task_signatures") or {})
    consumed_ids = set(read_cache.get("consumed_paper_ids") or [])
    for item in prior_readings:
        paper_id = _text(item.get("paper_id"))
        if paper_id:
            read_materials[paper_id] = dict(item)
            signature = _material_task_signature(item)
            if signature:
                read_task_signatures[paper_id] = signature
            consumed_ids.add(paper_id)

    def run(*, phase: str, supplement_requests: Sequence[Mapping[str, Any]] = (),
            directed_requests: Sequence[Mapping[str, Any]] = (), pool_rows: Sequence[Mapping[str, Any]] = (),
            plan: Mapping[str, Any] | None = None, prior_tool_results: Mapping[str, Any] | None = None,
            prior_directed: Mapping[str, Any] | None = None, source_handle_map: Mapping[str, str] | None = None,
            resume: bool = True, output_dir: str | Path | None = None, **_: Any) -> Mapping[str, Any]:
        from .planning_retrieval_loop import (
            InformationNeed, LoopConfig, need_id_for, needs_from_planner_gap_rows, run_retrieval_loop,
        )

        root = Path(output_dir or config.output_dir / str(phase).lower())
        root.mkdir(parents=True, exist_ok=True)
        topic = _text((plan or {}).get("research_question") or (plan or {}).get("question_en") or (plan or {}).get("question") or config.topic_id)
        gaps = [_normalize_gaps([row])[0] for row in supplement_requests if isinstance(row, Mapping)]
        tasks = [
            {**dict(task), "_progressive_task_signature": _directed_task_signature(task)}
            for task in _merge_directed_tasks(directed_requests)
        ]
        # A query plan may supply an explicitly linked facet when the gap itself
        # has no query. Do not borrow unrelated facets or invent domain terms.
        for request in gaps:
            if not request.get("targeted_queries") and request.get("reuse_plan_facet_ids"):
                ids = set(request["reuse_plan_facet_ids"])
                linked = []
                for facet in (plan or {}).get("facets") or ():
                    if isinstance(facet, Mapping) and facet.get("id") in ids:
                        for key, kind in (("keyword_queries", "keyword"), ("question_queries", "question")):
                            linked.extend({"query_text": text, "query_type": kind, "facet_id": facet["id"]}
                                          for text in facet.get(key) or [] if _text(text))
                request["targeted_queries"] = linked[:3]
        for request in gaps:
            for row in request.get("known_papers") or []:
                if isinstance(row, dict) and not (row.get("canonical_paper_id") or row.get("paper_id")):
                    paper_id = _text((source_handle_map or {}).get(_text(row.get("source_handle"))))
                    if paper_id:
                        row["paper_id"] = paper_id
            nominated = {_text(row.get("canonical_paper_id") or row.get("paper_id"))
                         for row in request.get("known_papers") or [] if isinstance(row, Mapping)}
            for handle in request.get("known_paper_handles") or []:
                paper_id = _text((source_handle_map or {}).get(_text(handle)))
                if paper_id and paper_id not in nominated:
                    request["known_papers"].append({"paper_id": paper_id})
                    nominated.add(paper_id)
        index_path = Path(local_index_path) if local_index_path else config.output_dir / "planning_material_index.sqlite"
        # Conservative generation guard for journal replay. The durable judge
        # cache below fingerprints actual material, so WAL checkpoint/stat-only
        # changes may re-inspect but cannot repay identical model inputs.
        local_index_generation = []
        for path in (index_path, Path(str(index_path) + "-wal")):
            if path.is_file():
                stat = path.stat()
                if path != index_path and stat.st_size == 0:
                    continue  # read-only SQLite can create an empty WAL
                local_index_generation.append([str(path.resolve()), stat.st_ino, stat.st_size, stat.st_mtime_ns])
        needs = needs_from_planner_gap_rows(gaps, intended_use="mechanism", user_scope=topic)
        kind_by_id: dict[str, str] = {}
        request_by_id: dict[str, Mapping[str, Any]] = {}
        cache_path = config.output_dir / "RETRIEVAL_NEED_CACHE.json"
        saved_cache = _read_json(cache_path) if cache_path.is_file() else {}
        cache_records = dict(saved_cache.get("records") or {}) if saved_cache.get("schema_version") == "optomind.retrieval_need_cache.v1" else {}
        cache_records = {key: row for key, row in cache_records.items() if isinstance(row, Mapping)
                         and isinstance(row.get("state"), Mapping) and isinstance(row.get("contract"), Mapping)
                         and isinstance(row.get("source_fingerprints"), Mapping)}
        contract_by_id: dict[str, dict[str, Any]] = {}
        inherited_by_id: dict[str, list[Mapping[str, Any]]] = {}
        cached_answer_by_id: dict[str, Mapping[str, Any]] = {}
        current_sources = {_text(row.get("_paper_id") or _canonical_paper_id(row)): row
                           for row in pool_rows if isinstance(row, Mapping)}

        def lookup_compatible(cached: Mapping[str, Any], nominations: list[str]) -> bool:
            return (cached.get("local_index_generation", local_index_generation) == local_index_generation
                    and cached.get("local_nominations", nominations) == nominations)

        def source_compatible(cached: Mapping[str, Any]) -> bool:
            # A conservative index-generation change requires a fresh look but
            # cannot discard earlier useful material from compatible sources.
            fingerprints = cached.get("source_fingerprints")
            if not isinstance(fingerprints, Mapping) or not all(
                paper_id not in current_sources or _supplement_source_fingerprint(current_sources[paper_id]) == signature
                for paper_id, signature in fingerprints.items()
            ):
                return False
            for paper_id, old in (cached.get("supplement_fingerprints") or {}).items():
                current = current_sources.get(paper_id)
                if not current or not any(key in current for key in ("supplement_gap_material", "supplement_gap_materials", "supplement_source_unit_id")):
                    # A caller may still supply its unchanged original pool;
                    # absence of our additive output is not a source correction.
                    continue
                fresh = _supplement_material_fingerprints(current)
                if old.get("source_unit_id") == fresh["source_unit_id"] and old.get("active") != fresh["active"]:
                    return False
                if not set(old.get("materials") or []) <= set(fresh["materials"]):
                    return False
            return True

        for need, request in zip(needs, gaps):
            contract = _supplement_need_contract(request, need)
            need.success_criteria = tuple(sorted(set(
                _supplement_contract_token(item) for item in
                [*(request.get("success_criteria") or []), *(request.get("required_outputs") or [])]
            )))
            related = [row for row in cache_records.values() if isinstance(row, Mapping)
                       and isinstance(row.get("contract"), Mapping)
                       and row["contract"].get("research") == contract["research"]
                       and row["contract"].get("research_key") == contract["research_key"]]
            nominations = sorted({_text(row.get("canonical_paper_id") or row.get("paper_id"))
                                  for row in request.get("known_papers") or [] if isinstance(row, Mapping)
                                  and _text(row.get("canonical_paper_id") or row.get("paper_id"))})
            used_ids = {paper_id for row in related for paper_id in (row.get("source_fingerprints") or {})}
            used_ids.update(_text(row.get("canonical_paper_id") or row.get("paper_id"))
                            for row in request.get("known_papers") or [] if isinstance(row, Mapping))
            source_context = {paper_id: {
                "source": _supplement_source_fingerprint(current_sources[paper_id]),
                "supplement": _supplement_material_fingerprints(current_sources[paper_id]),
            } for paper_id in sorted(used_ids) if paper_id in current_sources}
            effective_sources = dict(source_context)
            if local_index_generation or nominations:
                effective_sources["local_lookup"] = {"index_generation": local_index_generation, "nominations": nominations}
            need.need_id = "N" + contract["reuse_key"][:20] + (
                "-" + _material_content_signature(effective_sources) if effective_sources else "")
            contract_by_id[need.need_id] = contract
            kind_by_id[need.need_id] = "supplement"
            inherited = [row for row in related if source_compatible(row)]
            inherited_by_id[need.need_id] = inherited
            exact = cache_records.get(contract["reuse_key"]) or {}
            if exact.get("contract") == contract and source_compatible(exact) and lookup_compatible(exact, nominations) and _supplement_answered(exact.get("state") or {}):
                cached_answer_by_id[need.need_id] = exact["state"]
            covered = {key: set() for key in ("success_criteria", "required_outputs")}
            for row in inherited:
                if lookup_compatible(row, nominations) and _supplement_answered(row.get("state") or {}):
                    for key in covered:
                        covered[key].update((row.get("contract") or {}).get("acceptance", {}).get(key) or [])
            delta = dict(request)
            for key in covered:
                delta[key] = [item for item in request.get(key) or []
                              if _supplement_contract_token(item) not in covered[key]]
            delta["local_acceptance_delta"] = any(len(delta[key]) != len(request.get(key) or []) for key in covered)
            # Only prior answered requirements may be removed. Partial prose
            # stays available but never supplies completion by its shape.
            delta["reusable_material"] = "\n\n".join(dict.fromkeys(
                _text(row.get("state", {}).get("usable_content")) for row in inherited
                if _text(row.get("state", {}).get("usable_content"))))
            if inherited and not any(delta[key] for key in covered):
                answered_states = [row["state"] for row in inherited if lookup_compatible(row, nominations) and _supplement_answered(row.get("state") or {})]
                if answered_states:
                    cached_answer_by_id[need.need_id] = {
                        **dict(answered_states[0]), "status": "answered", "still_missing": "",
                        "usable_content": "\n\n".join(dict.fromkeys(row["usable_content"] for row in answered_states)),
                    }
            delta["still_missing"] = "; ".join(dict.fromkeys(
                _text(row.get("state", {}).get("still_missing")) for row in inherited
                if not _supplement_answered(row.get("state") or {}) and _text(row.get("state", {}).get("still_missing"))))
            request_by_id[need.need_id] = delta
        for task in tasks:
            paper_id = _text(task.get("paper_id"))
            questions = [
                _text(item.get("question") if isinstance(item, Mapping) else item)
                for item in task.get("questions") or ()
            ]
            question = "; ".join(item for item in questions if item) or "; ".join(task.get("knowledge_gaps") or task.get("reasons") or [])
            if not question:
                continue
            need_id = _text(task.get("need_id")) or need_id_for(question + (" || " + paper_id if paper_id else "") + " || " + _directed_task_signature(task))
            current_source = current_sources.get(paper_id)
            if current_source is not None:
                need_id += "-" + _material_content_signature(_directed_source_state(current_source))
            round_specs = tuple(task.get("round_specs") or ())
            need = InformationNeed(
                need_id=need_id,
                question=question,
                owners=tuple(_text(item) for item in task.get("chapter_ids") or () if _text(item)),
                intended_use="study_design",
                kind="directed",
                user_scope=topic,
                success_criteria=tuple(_text(item) for item in task.get("required_outputs") or () if _text(item)),
                concepts=tuple(_text(item) for item in task.get("concepts") or () if _text(item)),
                round_specs=tuple(dict(item) for item in round_specs if isinstance(item, Mapping)),
                existing_handles=tuple(_text(item) for item in task.get("existing_handles") or () if _text(item)),
            )
            needs.append(need)
            kind_by_id[need_id] = "directed"
            request_by_id[need_id] = task

        if not needs:
            return {"phase": phase, "status": "not_requested", "supplement_results": [], "directed_results": [],
                    "retrieval_loop": {"needs": []}, "tool_materials_by_chapter": {}}

        index_path = Path(local_index_path) if local_index_path else config.output_dir / "planning_material_index.sqlite"
        judge = None
        if key_file and budget_ledger_path and budget_limit_cny is not None:
            from .planning_material_triage import QwenLocalTriageJudge
            judge = QwenLocalTriageJudge(
                key_file=key_file, budget_ledger_path=budget_ledger_path,
                budget_limit_cny=float(budget_limit_cny), model=config.reader_model,
                timeout_seconds=config.timeout_seconds,
                raw_response_dir=root / "_llm_response_cache",
            )
        working_pool = [dict(row) for row in pool_rows]
        for inherited in inherited_by_id.values():
            for cached in inherited:
                _merge_supplement_pool_updates(working_pool, {"supplement_results": cached.get("state", {}).get("external_results") or []}, preserve_current_material=True)
        journal_path = root / "retrieval_loop.jsonl"
        if resume and journal_path.is_file():
            for line in journal_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if _text(entry.get("need_id")) not in request_by_id:
                    continue
                recovered = entry.get("external_results") or [entry.get("external_result")]
                for result in recovered:
                    if isinstance(result, Mapping):
                        _merge_supplement_pool_updates(working_pool, {"supplement_results": [result]}, preserve_current_material=True)
        local_results: dict[str, Mapping[str, Any]] = {}
        external_results: dict[str, list[Mapping[str, Any]]] = {}
        explicit_retry_consumed: set[str] = set()
        local_retry_consumed: set[str] = set()
        pool_by_id = {str(row.get("_paper_id")): dict(row) for row in working_pool if isinstance(row, Mapping)}
        paper_to_handle = {str(value): str(key) for key, value in (source_handle_map or {}).items()}

        def retain_local_context(need_id: str, result: Mapping[str, Any]) -> dict[str, Any]:
            result = dict(result)
            retained = _text((request_by_id.get(need_id) or {}).get("reusable_material"))
            if retained:
                result["usable_content"] = "\n\n".join(dict.fromkeys(
                    text for text in (retained, _text(result.get("usable_content"))) if text))
                contexts = [_local_writer_context(cached["state"].get("local_triage") or {}, omit_gap_limit=True)
                            for cached in inherited_by_id.get(need_id, [])
                            if _text(cached["state"].get("usable_content"))]
                writer = _local_writer_context(result)
                for key in ("sources", "conditions", "limits", "allowed_use"):
                    combined = {}
                    for context in [*contexts, writer]:
                        for item in context.get(key) or []:
                            combined[json.dumps(item, ensure_ascii=False, sort_keys=True, default=_json_default)] = item
                    if combined:
                        writer[key] = list(combined.values())
                if writer:
                    result["writer_material"] = writer
            local_results[need_id] = result
            return result

        def local_triage(*, gap: Any, round_index: int) -> Mapping[str, Any]:
            from .planning_supplement import run_gap_local_triage
            cached = cached_answer_by_id.get(gap.gap_id)
            if cached:
                result = {**dict(cached.get("local_triage") or {}), "decision": "direct_use",
                          "usable_content": cached["usable_content"], "still_missing": "",
                          "answers_requested_question": True, "reused_answer": True,
                          "writer_material": _local_writer_context(cached.get("local_triage") or {}, omit_gap_limit=True)}
                return retain_local_context(gap.gap_id, result)
            if kind_by_id.get(gap.gap_id) == "directed":
                return {"decision": "external_research", "usable_content": "", "still_missing": gap.question,
                        "external_ask": "Read the nominated paper for this task."}
            prior = request_by_id.get(gap.gap_id) or {}
            pending_criteria = [*(prior.get("success_criteria") or []), *(prior.get("required_outputs") or [])]
            gap_row = {
                "gap_id": gap.gap_id, "gap_question": gap.question, "question": gap.question,
                "intended_use": gap.intended_use, "user_scope": gap.user_scope,
                "success_criteria": ([_supplement_requirement_text(item) for item in pending_criteria]
                    if prior.get("local_acceptance_delta") else list(gap.success_criteria)),
                "chapter_ids": list(gap.chapter_ids),
                "targeted_queries": [{"query_text": item, "query_type": "keyword"} for item in gap.concepts],
                "required_concepts": list(gap.required_concepts),
                "known_paper_handles": list(prior.get("known_paper_handles") or gap.existing_handles),
                "known_papers": list(prior.get("known_papers") or []),
                "reusable_material": _text(prior.get("reusable_material")) if prior.get("local_acceptance_delta") else "",
            }
            # A planner may supply a Chinese question without English query
            # terms. Keep that question for the reader, but use the existing
            # topic vocabulary to obtain local candidates instead of crashing.
            from .planning_material_search import expand_query
            if not expand_query(" ".join([gap.question, *gap.concepts, *gap.required_concepts])):
                gap_row["targeted_queries"] = [
                    {"query_text": form, "query_type": "keyword"}
                    for term in expand_query(topic) for form in term.forms
                ]
            if not index_path.is_file():
                result = {"gap_id": gap.gap_id, "decision": "external_research", "usable_content": "",
                          "still_missing": gap.question, "external_ask": gap.question}
                prior = request_by_id.get(gap.gap_id) or {}
                result["usable_content"] = _text(prior.get("reusable_material"))
                result["still_missing"] = _text(prior.get("still_missing")) or "; ".join(_supplement_requirement_text(item) for item in
                    [*(prior.get("success_criteria") or []), *(prior.get("required_outputs") or [])]) or gap.question
                return retain_local_context(gap.gap_id, result)
            kwargs = {
                "index_path": index_path, "user_scope": topic, "judge": judge,
                "output_dir": root / "local_triage" / _safe_id(gap.gap_id), "read_local": True,
                "source_handle_map": dict(source_handle_map or {}),
                "cache_dir": config.output_dir / "local_lookup",
                "retry_failed": prior.get("retry_empty_result") is True and gap.gap_id not in local_retry_consumed,
            }
            if kwargs["retry_failed"]:
                local_retry_consumed.add(gap.gap_id)
            import inspect
            parameters = inspect.signature(run_gap_local_triage).parameters
            if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
                kwargs = {key: value for key, value in kwargs.items() if key in parameters}
            result = run_gap_local_triage(gap_row, **kwargs)
            # Keep the current judgment authoritative for completion while
            # inherited prose retains its own source and scientific context.
            return retain_local_context(gap.gap_id, result)

        def refine_queries(need: Any, round_index: int, previous_queries: Sequence[Mapping[str, Any]],
                           usable_content: str, still_missing: str) -> list[dict[str, Any]]:
            if need.kind == "directed" or not allow_external or not key_file or not budget_ledger_path or budget_limit_cny is None:
                return []
            from .module4.runtime import GlobalBudgetLedger, QwenDirectClient, invoke_client
            from .planning_material_triage import QwenLocalTriageJudge
            client = QwenDirectClient(model=config.reader_model, key_file=key_file,
                                      budget_ledger=GlobalBudgetLedger(limit_cny=budget_limit_cny, path=Path(budget_ledger_path)),
                                      max_output_tokens=1600, thinking=True, thinking_budget=1024,
                                      json_mode=config.reader_model == "qwen3.7-flash", max_retries=0,
                                      timeout_seconds=config.timeout_seconds, raw_response_dir=root / "query_refinements")
            response = invoke_client(client, [
                {"role": "system", "content": "你为综述的一个具体缺口改写检索式。根据已经找到的内容和仍缺的问题，换一个有希望的窄方向。只返回至多2条英语查询：keyword用研究对象或材料+具体关系，question用包含对象、关系和必要设置的完整语义陈述。不要把缺口说明整段拼进查询，不重复已查过的方向，不用没有研究对象的通用问句。保持综述主题与实际比较、方法或条件；找不到某种证据时不偷换对象。若现有材料足够调整写法或再查价值小，返回空列表。JSON {targeted_queries:[{query_type:keyword或question,query_text:...}],reason:...}。"},
                {"role": "user", "content": json.dumps({"review_scope": need.user_scope, "question": need.question,
                     "previous_queries": previous_queries, "useful_material": usable_content,
                     "still_missing": still_missing, "next_round": round_index}, ensure_ascii=False)},
            ], model=config.reader_model, max_output_tokens=1600, thinking=True, thinking_budget=1024,
               call_id=f"query-refinement:{need.need_id}:{round_index}")
            decoded = QwenLocalTriageJudge._decode(response.get("content"))
            return [dict(item) for item in decoded.get("targeted_queries") or [] if isinstance(item, Mapping)][:2]

        def external_closure(*, need: Any, round_index: int, queries: Sequence[Mapping[str, Any]], spec: Mapping[str, Any], **budget_context: Any) -> Mapping[str, Any]:
            request = dict(request_by_id.get(need.need_id) or {})
            # An explicit retry authorizes one new attempt, not a retry per round.
            request["retry_empty_result"] = request.get("retry_empty_result") is True and need.need_id not in explicit_retry_consumed
            if request["retry_empty_result"]:
                explicit_retry_consumed.add(need.need_id)
            context = {
                "phase": phase, "output_dir": root / "external" / _safe_id(need.need_id) / f"round_{round_index}",
                "topic_id": config.topic_id, "plan": dict(plan or {}), "pool_rows": working_pool,
                "pool_by_id": pool_by_id, "source_handle_map": dict(source_handle_map or {}),
                "prior_tool_results": dict(prior_tool_results or {}), "prior_directed": dict(prior_directed or {}),
                "shared_deep_read_budget": config.shared_deep_read_budget,
                "skip_local_triage": True,
            }
            if kind_by_id.get(need.need_id) == "supplement":
                if supplement_runner is None or not allow_external:
                    return {"status": "unmet", "usable_content": "", "still_missing": need.question}
                request["targeted_queries"] = [dict(item) for item in queries if isinstance(item, Mapping)]
                plan_facets = [_text(item.get("id")) for item in (plan or {}).get("facets") or ()
                               if isinstance(item, Mapping) and _text(item.get("id"))]
                requested_facets = [item for item in request.get("reuse_plan_facet_ids") or [] if item in plan_facets]
                original_facets = [_text(item.get("facet_id")) for item in request_by_id.get(need.need_id, {}).get("targeted_queries") or []
                                   if isinstance(item, Mapping) and _text(item.get("facet_id"))]
                routing_facet = next(iter(requested_facets or original_facets or plan_facets), "F1")
                for query in request["targeted_queries"]:
                    if not _text(query.get("facet_id")):
                        query["facet_id"] = routing_facet
                raw = dict(supplement_runner([request], **context))
                before = {str(row.get("_paper_id")): json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_default) for row in working_pool}
                _merge_supplement_pool_updates(working_pool, {"supplement_results": [raw]})
                pool_by_id.update({str(row.get("_paper_id")): row for row in working_pool})
                changed = [row for row in working_pool if before.get(str(row.get("_paper_id"))) != json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_default)]
                _extend_planning_material_index(index_path, changed, root / "index_updates" / _safe_id(need.need_id) / str(round_index), config.topic_id)
                external_results.setdefault(need.need_id, []).append(raw)
                from .practical_materials import has_practical_content
                groups: list[Mapping[str, Any]] = []
                pending = [raw]
                while pending:
                    group = pending.pop(0)
                    if not isinstance(group, Mapping):
                        continue
                    groups.append(group)
                    pending.extend(group.get("results") or [])
                def content_aliases(value: Any) -> Any:
                    if isinstance(value, Mapping):
                        return {({"summary": "explanation", "mechanisms": "details"}.get(key, key)): content_aliases(item)
                                for key, item in value.items()}
                    if isinstance(value, (list, tuple)):
                        return [content_aliases(item) for item in value]
                    return value

                useful_values: list[str] = []
                statuses: set[str] = set()
                remaining_values: list[str] = []
                failure_errors: list[str] = []
                for group in groups:
                    judgment = group.get("fulfillment_judgment")
                    observations = [group, judgment] if isinstance(judgment, Mapping) else [group]
                    for item in observations:
                        item_status = _text(item.get("substantive_gap_status") or item.get("status")).casefold()
                        if item_status in {"fulfilled", "partial", "unmet", "failed", "provider_error", "provider_failed", "unjudgeable", "unassessed"}:
                            statuses.add(item_status)
                        for key in ("usable_content", "useful_material", "auxiliary_material"):
                            value = item.get(key)
                            if value and has_practical_content({"useful_material": content_aliases(value)}):
                                useful_values.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
                        remaining_values.extend(_text(item.get(key)) for key in ("remaining_gap", "still_missing") if _text(item.get(key)))
                        if item_status in {"failed", "provider_error", "provider_failed"} or item.get("provider_failed") or item.get("outcome") in {
                            "retryable_provider_outage", "provider_access_error", "acquisition_or_judgment_failed"
                        }:
                            failure_errors.append(_text(item.get("error") or item.get("reason") or "provider_failed"))
                useful = "\n\n".join(dict.fromkeys(useful_values))
                remaining = "; ".join(dict.fromkeys(remaining_values))
                # A fulfilled child cannot close another unresolved child, nor
                # can a nonempty artifact/identifier substitute for an answer.
                answered = bool(useful) and statuses == {"fulfilled"} and not remaining
                status = "fulfilled" if answered else "partial" if useful else "failed" if failure_errors else "unmet"
                if status != "fulfilled" and not remaining:
                    remaining = _text(request.get("still_missing")) or need.question
                return {"status": status, "usable_content": useful, "still_missing": remaining,
                        "new_handles": [], "raw_result": raw, "provider_failed": bool(failure_errors and not useful),
                        "error": "; ".join(dict.fromkeys(failure_errors))}
            # External retrieval can be disabled while reading an already
            # downloaded paper remains available under the shared read budget.
            if directed_reader is None:
                return {"status": "unmet", "usable_content": "", "still_missing": need.question}
            paper_id = _text(request.get("paper_id"))
            task_signature = _directed_task_signature(request)
            prior_material = read_materials.get(paper_id)
            prior_signature = read_task_signatures.get(paper_id) or _material_task_signature(prior_material)
            if prior_material is not None and prior_signature and prior_signature == task_signature \
                    and _directed_material_compatible(request, prior_material) \
                    and _prior_read_source_compatible(pool_by_id.get(paper_id, {}), prior_material):
                raw = {"status": "reused_prior_deep_read", "materials": [dict(prior_material)],
                       "consumed_paper_ids": [paper_id], "task_reused": True}
            elif paper_id not in consumed_ids and len(consumed_ids) >= config.shared_deep_read_budget:
                return {"status": "unmet", "usable_content": "", "still_missing": need.question,
                        "outcome": "shared_deep_read_budget_exhausted"}
            else:
                raw = dict(directed_reader([request], **context))
                consumed_ids.update(raw.get("consumed_paper_ids") or [])
                decorated_materials: list[dict[str, Any]] = []
                for item in raw.get("materials") or []:
                    if isinstance(item, Mapping) and _text(item.get("paper_id")):
                        decorated = _decorate_directed_material(
                            item, task=request, prior_material=prior_material,
                        )
                        item_paper_id = _text(item.get("paper_id"))
                        read_materials[item_paper_id] = decorated
                        read_task_signatures[item_paper_id] = task_signature
                        decorated_materials.append(decorated)
                raw["materials"] = decorated_materials
                _atomic_json(read_cache_path, {
                    "materials": read_materials,
                    "task_signatures": read_task_signatures,
                    "consumed_paper_ids": sorted(consumed_ids),
                })
            external_results.setdefault(need.need_id, []).append(raw)
            materials = raw.get("materials") or []
            material_statuses = [_directed_material_status(request, item) for item in materials if isinstance(item, Mapping)]
            useful = " ".join(
                json.dumps({
                    **(dict(item["content"]) if isinstance(item.get("content"), Mapping) else {}),
                    "question_material": _material_current_question_rows(item),
                    "plain_text": _text(item.get("plain_text")),
                }, ensure_ascii=False)
                for item in materials
                if isinstance(item, Mapping) and _directed_material_status(request, item) != "unmet"
            )
            status = "fulfilled" if "fulfilled" in material_statuses else "partial" if useful else "unmet"
            raw_status = _text(raw.get("status")).casefold()
            provider_failed = bool(raw.get("provider_failed")) or raw_status in {"failed", "provider_failed", "provider_error"}
            if provider_failed and not useful:
                status = "failed"
            added = [_text(item.get("paper_id")) for item in materials if isinstance(item, Mapping) and _text(item.get("paper_id"))]
            return {"status": status, "still_missing": "" if status == "fulfilled" else need.question,
                    "usable_content": useful, "new_handles": added, "raw_result": raw,
                     "consumed_paper_ids": list(raw.get("consumed_paper_ids") or []),
                     "provider_failed": provider_failed and not useful,
                     "error": _text(raw.get("error") or raw.get("reason")) if provider_failed and not useful else ""}

        loop_config = LoopConfig(
            index_path=index_path, journal_path=root / "retrieval_loop.jsonl",
            max_rounds=3, max_empty_rounds=2, max_queries_per_round=3,
            shared_deep_read_budget=config.shared_deep_read_budget,
        )
        loop_result = run_retrieval_loop(
            needs, loop_config, local_triage=local_triage, external_closure=external_closure,
            resume=resume, refine_queries=refine_queries, prior_consumed_paper_ids=sorted(consumed_ids),
            retry_need_ids=[need_id for need_id, request in request_by_id.items()
                            if request.get("retry_empty_result") is True and need_id not in cached_answer_by_id],
        )
        materials_by_chapter: dict[str, list[dict[str, Any]]] = {}
        supplement_results: list[dict[str, Any]] = []
        directed_results: list[dict[str, Any]] = []
        for need_state in loop_result.get("needs") or []:
            need_id = _text(need_state.get("need_id"))
            need_info = need_state.get("need") if isinstance(need_state.get("need"), Mapping) else {}
            owners = [_text(item) for item in need_info.get("owners") or need_state.get("owners") or () if _text(item)]
            local = need_state.get("local_triage") if isinstance(need_state.get("local_triage"), Mapping) else local_results.get(need_id, {})
            restored = [entry for entry in need_state.get("external_results") or [] if isinstance(entry, Mapping)]
            delivered = ([*restored, *external_results.get(need_id, [])] if kind_by_id.get(need_id) == "supplement"
                         else restored or external_results.get(need_id, []))
            raw_results = [dict(item) for item in delivered if isinstance(item, Mapping)]
            if kind_by_id.get(need_id) == "supplement":
                prior_raw = [result for cached in inherited_by_id.get(need_id, [])
                             for result in cached.get("state", {}).get("external_results") or [] if isinstance(result, Mapping)]
                unique = {}
                for result in _supplement_bind_results([*prior_raw, *raw_results], owners):
                    unique[json.dumps(result, ensure_ascii=False, sort_keys=True, default=_json_default)] = result
                raw_results = list(unique.values())
                need_state["external_results"] = raw_results
                contract = contract_by_id[need_id]
                key = contract["reuse_key"]
                previous = cache_records.get(key) or {}
                bindings = list(previous.get("owner_bindings") or [])
                binding = {"phase": phase, "owners": owners}
                if binding not in bindings:
                    bindings.append(binding)
                fingerprints = {
                    paper_id: _supplement_source_fingerprint(current_sources.get(paper_id) or pool_by_id[paper_id])
                    for paper_id in _supplement_used_papers(need_state)
                    if paper_id in current_sources or paper_id in pool_by_id
                }
                if previous and previous.get("source_fingerprints") != fingerprints:
                    history_key = key + ":prior:" + _material_content_signature(previous)
                    cache_records.setdefault(history_key, previous)
                supplement_fingerprints = {paper_id: _supplement_material_fingerprints(pool_by_id[paper_id])
                                           for paper_id in fingerprints if paper_id in pool_by_id}
                cache_records[key] = {"contract": contract, "state": dict(need_state),
                                      "source_fingerprints": fingerprints, "supplement_fingerprints": supplement_fingerprints,
                                      "owner_bindings": bindings,
                                      "local_index_generation": local_index_generation,
                                      "local_nominations": sorted({_text(row.get("canonical_paper_id") or row.get("paper_id"))
                                          for row in request_by_id.get(need_id, {}).get("known_papers") or [] if isinstance(row, Mapping)
                                          and _text(row.get("canonical_paper_id") or row.get("paper_id"))})}
                need_state["reuse_key"] = key
                need_state["reused_answer"] = need_id in cached_answer_by_id
            raw = raw_results[-1] if raw_results else {}
            if raw_results:
                if kind_by_id.get(need_id) == "supplement":
                    supplement_results.extend(raw_results)
                else:
                    directed_results.extend(raw_results)
            usable = _text(need_state.get("usable_content") or local.get("usable_content"))
            writer = _local_writer_context(local, omit_gap_limit=_supplement_answered(need_state))
            sources = writer.get("sources") if isinstance(writer.get("sources"), list) else local.get("passages") or []
            if raw_results and kind_by_id.get(need_id) == "directed":
                directed_materials = [
                    item for result in raw_results for item in (result.get("materials") or [])
                    if isinstance(item, Mapping) and _text(item.get("paper_id"))
                ]
                sources = [
                    {"paper_id": _text(item.get("paper_id")),
                     "source_handle": paper_to_handle.get(_text(item.get("paper_id"))),
                     "title": _text(item.get("title") or item.get("paper_title"))}
                    for item in directed_materials
                ] or sources
            if raw_results and kind_by_id.get(need_id) == "supplement":
                pending = list(raw_results)
                collected_sources: list[Mapping[str, Any]] = []
                collected_values: list[str] = []
                while pending:
                    group = pending.pop(0)
                    if not isinstance(group, Mapping):
                        continue
                    pending.extend(group.get("results") or [])
                    judgment = group.get("fulfillment_judgment") if isinstance(group.get("fulfillment_judgment"), Mapping) else {}
                    values = judgment.get("useful_material") or judgment.get("auxiliary_material") or []
                    if isinstance(values, str):
                        values = [values]
                    if isinstance(values, (list, tuple)):
                        collected_values.extend(str(item) for item in values if str(item).strip())
                    collected_sources.extend(item for item in group.get("source_units") or () if isinstance(item, Mapping))
                if not usable:
                    usable = " ".join(dict.fromkeys(collected_values))
                sources = [*sources, *[{**dict(item.get("record_identity") or {}), **dict(item)} for item in collected_sources]]
            if usable or writer:
                material = dict(writer)
                material.update({"need_id": need_id, "chapter_ids": owners, "question": _text(need_info.get("question")),
                                 "decision": _text(need_state.get("action") or local.get("decision")),
                                 "usable_content": usable,
                                 # A completed closure explicitly clears this field. Do not
                                 # revive the earlier local lookup's unresolved question.
                                 "still_missing": _text(need_state["still_missing"] if "still_missing" in need_state
                                                        else local.get("still_missing")),
                                 "sources": [dict(item) for item in sources if isinstance(item, Mapping)]})
                for owner in owners:
                    materials_by_chapter.setdefault(owner, []).append(dict(material))
        if contract_by_id:
            _atomic_json(cache_path, {"schema_version": "optomind.retrieval_need_cache.v1", "records": cache_records})
        return {
            "phase": phase, "status": "complete", "retrieval_loop": loop_result,
            "supplement_results": supplement_results, "directed_results": directed_results,
            "tool_materials_by_chapter": materials_by_chapter,
            "consumed_paper_ids": sorted(consumed_ids),
        }
    return run


def make_directed_reading_runner(
    config: ProgressivePlannerConfig,
    *,
    key_file: str | Path,
    budget_ledger_path: str | Path,
    budget_limit_cny: float,
    prior_readings: Sequence[Mapping[str, Any]] = (),
) -> Callable[..., Mapping[str, Any]]:
    """Build a callable adapter over existing snapshots and DirectedReadingStore."""
    def run(tasks: Sequence[Mapping[str, Any]], **context: Any) -> Mapping[str, Any]:
        if not tasks:
            return {"status": "not_requested", "materials": [], "consumed_paper_ids": [], "attempted_paper_ids": []}
        from .directed_reading import DirectedReadingStore, build_directed_request, run_directed_reading

        phase_root = Path(context.get("output_dir") or config.output_dir / str(context.get("phase") or "tools"))
        store = DirectedReadingStore(config.output_dir / "directed_reading.sqlite", core_cap=max(1, config.shared_deep_read_budget))
        plan = context.get("plan") if isinstance(context.get("plan"), Mapping) else {}
        topic = _text(plan.get("research_question") or plan.get("review_title") or plan.get("question_en") or plan.get("question") or config.topic_id)
        topic_binding = "progressive-review:" + config.topic_id
        pool_by_id = context.get("pool_by_id") if isinstance(context.get("pool_by_id"), Mapping) else {}
        results: list[dict[str, Any]] = []
        prior_by_id = {str(row.get("paper_id")): dict(row) for row in prior_readings if isinstance(row, Mapping) and _text(row.get("paper_id"))}

        def normalize_question_rows(task: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            normalized = _directed_task_requirements(task)
            questions = []
            for row in normalized["questions"]:
                question = dict(row)
                gap_key = ""
                for raw in task.get("questions") or ():
                    if isinstance(raw, Mapping) and _text(raw.get("question")) == row["question"]:
                        gap_key = _text(raw.get("gap_key") or raw.get("knowledge_gap"))
                        break
                question["gap_key"] = gap_key or "; ".join(normalized["knowledge_gaps"])
                questions.append(question)
            outputs = [dict(row) for row in normalized["required_outputs"]]
            return questions, outputs

        def read_one(task: Mapping[str, Any]) -> dict[str, Any]:
            paper_id = _text(task.get("paper_id"))
            candidate = pool_by_id.get(paper_id)
            if not isinstance(candidate, Mapping):
                return {"paper_id": paper_id, "status": "unavailable", "reason": "paper_not_in_current_pool"}
            questions, outputs = normalize_question_rows(task)
            signature_task = {
                **dict(task), "questions": questions, "required_outputs": outputs,
                "knowledge_gaps": task.get("knowledge_gaps") or task.get("knowledge_gap")
                or task.get("reasons") or task.get("reason") or [],
            }
            task_signature = _directed_task_signature(signature_task)
            reused = prior_by_id.get(paper_id)
            source_identity = _candidate_identity(candidate)
            if reused and _saved_card_identity_conflict(source_identity, reused) is not None:
                reused = None  # Different-paper text is not history for this source.
            current_source = _directed_source_state(candidate)
            if current_source.get("material_identity_conflict"):
                return {"paper_id": paper_id, "status": "unavailable", "reason": "source_identity_conflict"}
            if reused and _directed_material_compatible(signature_task, reused) and _prior_read_source_compatible(candidate, reused):
                return {
                    "paper_id": paper_id,
                    "status": "reused_prior_deep_read",
                    "material": _decorate_directed_material(reused, task=signature_task),
                }
            if candidate.get("root_review_note"):
                retained = {**dict(reused), "current_question_material": [],
                            "prior_reading_compatibility": "review_account_unverified"} if reused else {}
                return {"paper_id": paper_id, "status": "review_reported_no_reacquire",
                        "reason": "Use the attributed account already supplied by its source review.",
                        "blocked_paper_id": paper_id, **({"material": retained} if retained else {})}
            snapshot = _snapshot_for_candidate(candidate)
            if snapshot is None:
                retained = {**dict(reused), "prior_question_material": _material_question_rows(reused),
                            "current_question_material": [], "prior_reading_compatibility": "current_snapshot_unavailable"} if reused else {}
                return {"paper_id": paper_id, "status": "partial" if retained else "unavailable",
                        "reason": "existing_snapshot_not_found", **({"material": retained} if retained else {})}
            card = _card_for_candidate(candidate)
            identity = card.get("paper_identity") if isinstance(card.get("paper_identity"), Mapping) else {}
            a_summary = card.get("general_understanding") if isinstance(card.get("general_understanding"), Mapping) else {}
            planning = candidate.get("planning_view") if isinstance(candidate.get("planning_view"), Mapping) else {}
            planning_identity = planning.get("paper_identity") if isinstance(planning.get("paper_identity"), Mapping) else {}
            title = _text(identity.get("title") or planning_identity.get("title") or candidate.get("title"))
            chapter_ids = list(task.get("chapter_ids") or [])
            chapter_title = " / ".join(_text(item) for item in chapter_ids) or "Coordinated review"
            kind = _text(identity.get("paper_kind") or a_summary.get("paper_kind") or a_summary.get("study_type") or candidate.get("paper_kind") or "study")
            scope = _text((card.get("material") or {}).get("material_scope") if isinstance(card.get("material"), Mapping) else "") or planning.get("material_scope")
            nomination = {
                "canonical_paper_id": paper_id,
                "title": title,
                "doi": _text(source_identity.get("doi")),
                "paper_kind": kind,
                "material_scope": scope,
                "snapshot_dir": str(snapshot),
                "plan_path": str(config.plan_path),
                "nomination_reason": _text(task.get("reason") or task.get("knowledge_gap") or "Needed to resolve a specific coordinated review question."),
                "expected_information_gain": "Provides paper-specific findings, conditions, and limits for the requested comparison.",
                "core_justification": "The coordinated outline names this source for a specific evidence need.",
                "knowledge_gap": "; ".join(_text(item) for item in (task.get("knowledge_gaps") or [task.get("knowledge_gap") or task.get("reason") or "The coordinated outline identifies an unresolved evidence question."]) if _text(item)),
                "required_outputs": [row["output_id"] for row in outputs],
                "questions": questions,
                "approve_core": True,
                "substantive_review": "review" in kind.casefold() or "meta-analysis" in kind.casefold(),
                "metadata": {"chapter_ids": chapter_ids, "request_origin": str(context.get("phase") or "progressive_review")},
            }
            request = build_directed_request(
                review_id=config.topic_id,
                topic=topic,
                chapter={"chapter_id": "coordinated_scope", "title": chapter_title},
                questions=questions,
                required_outputs=outputs,
                candidates=[nomination],
                topic_binding=topic_binding,
            )
            admission = store.admit_candidates(config.topic_id, topic_binding, [nomination], topic_hash=config.topic_id)
            if not admission or admission[0].get("admission_status") != "approved_core":
                return {"paper_id": paper_id, "status": "unavailable", "reason": "directed_read_not_admitted", "admission": admission}
            task_payload = {"questions": questions, "required_outputs": outputs, "gap_keys": sorted(set(_directed_task_requirements(task)["knowledge_gaps"]) | {_text(row.get("gap_key")) for row in questions if _text(row.get("gap_key"))})}
            output_dir = phase_root / "directed" / _safe_id(paper_id)
            try:
                result = run_directed_reading(
                    request=request,
                    paper=nomination,
                    snapshot_dir=snapshot,
                    output_dir=output_dir,
                    store=store,
                    task=task_payload,
                    key_file=key_file,
                    budget_ledger_path=budget_ledger_path,
                    budget_limit_cny=budget_limit_cny,
                    max_output_tokens=config.chapter_output_tokens,
                    thinking_budget=config.thinking_budget,
                    retry_empty_result=task.get("retry_empty_result") is True or context.get("retry_empty_result") is True,
                )
                artifact = result.get("output") if isinstance(result.get("output"), Mapping) else {}
                material = _decorate_directed_material(artifact, task=signature_task, prior_material=reused)
                return {**dict(artifact), "paper_id": paper_id, "status": _directed_material_status(signature_task, artifact), "output_dir": str(result.get("output_dir") or output_dir), "material": material, "task_signature": task_signature, "reused": bool(result.get("reused")), "network_call": bool(result.get("network_call"))}
            except Exception as exc:
                return {"paper_id": paper_id, "status": "failed", "error": type(exc).__name__}

        with ThreadPoolExecutor(max_workers=max(1, config.reader_workers)) as executor:
            futures = [executor.submit(read_one, task) for task in tasks]
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda row: _text(row.get("paper_id")))
        attempted = sorted({_text(row.get("paper_id")) for row in results if _text(row.get("paper_id"))})
        consumed = sorted({_text(row.get("paper_id")) for row in results if row.get("status") in {"fulfilled", "partial", "unmet", "failed", "reused_prior_deep_read"} and _text(row.get("paper_id"))})
        blocked = [_text(row.get("blocked_paper_id")) for row in results if _text(row.get("blocked_paper_id"))]
        materials = [dict(row.get("material") or {}) for row in results if isinstance(row.get("material"), Mapping) and row.get("material")]
        failed_rows = [row for row in results if _text(row.get("status")).casefold() in {"failed", "provider_failed", "provider_error"}]
        has_useful = any(row.get("status") in {"fulfilled", "partial", "reused_prior_deep_read"} for row in results)
        aggregate_status = "fulfilled" if all(row.get("status") in {"fulfilled", "reused_prior_deep_read"} for row in results) else "partial" if has_useful else "unmet"
        provider_failed = bool(failed_rows and not has_useful)
        if provider_failed:
            aggregate_status = "failed"
        return {"status": aggregate_status, "provider_failed": provider_failed,
                "failed_paper_ids": sorted({_text(row.get("paper_id")) for row in failed_rows if _text(row.get("paper_id"))}),
                "results": results, "materials": materials, "consumed_paper_ids": consumed,
                "attempted_paper_ids": attempted, "blocked_paper_ids": blocked}
    return run


def _tool_material_keys(item: Mapping[str, Any]) -> list[str]:
    """One dedupe key per source handle inside a writer-material item."""

    sources = item.get("sources")
    handles: list[str] = []
    if isinstance(sources, (list, tuple)):
        for source in sources:
            if isinstance(source, Mapping) and _text(source.get("source_handle")):
                handles.append(_text(source["source_handle"]))
    if not handles and _text(item.get("source_handle")):
        handles.append(_text(item["source_handle"]))
    need = _text(item.get("need_id"))
    return [need + "|" + handle for handle in handles] or [need + "|"]


def _tool_material_for_prompt(item: Mapping[str, Any]) -> dict[str, Any]:
    """Compact a writer-material record for the chapter-details model input."""

    sources = []
    for source in item.get("sources") or ():
        if not isinstance(source, Mapping):
            continue
        sources.append({
            "source_handle": _text(source.get("source_handle")),
            **({key: source[key] for key in ("paper_id", "canonical_paper_id", "doi", "record_identity",
                                            "unresolved_source_handle", "identity_status") if source.get(key)}),
            "title": _text(source.get("title")),
            "year": _text(source.get("year")),
            "reading_role": _text(source.get("reading_role")),
            "material_depth": _text(source.get("material_depth")),
            "section_path": list(source.get("section_path") or []),
        })
    result = {
        "need_id": _text(item.get("need_id")),
        "question": _text(item.get("question")),
        "intended_use": _text(item.get("intended_use")),
        "decision": _text(item.get("decision")),
        "usable_content": _text(item.get("usable_content")),
        "conditions": list(item.get("conditions") or []),
        "limits": list(item.get("limits") or []),
        "still_missing": _text(item.get("still_missing")),
        "sources": sources,
        "allowed_use": list(item.get("allowed_use") or []),
    }

    if item.get("provider_failed"):
        result.update({key: item[key] for key in ("status", "provider_failed", "error", "failure_stage") if key in item})
    return result


def _source_keys_in_value(value: Any) -> tuple[set[str], set[str]]:
    """Collect source handles and paper IDs actually cited by a chapter plan."""
    handles: set[str] = set()
    paper_ids: set[str] = set()

    def add(target: set[str], raw: Any) -> None:
        if isinstance(raw, Mapping):
            raw = raw.get("source_handle") or raw.get("reference_handle") or raw.get("paper_id") or raw.get("canonical_paper_id")
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                add(target, item)
            return
        text = _text(raw)
        if text:
            target.add(text)

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                if key in {"source_handle", "reference_handle", "source_handles", "reference_handles"}:
                    add(handles, child)
                elif key in {"source_id", "source_ids", "paper_id", "paper_ids", "canonical_paper_id"}:
                    add(paper_ids, child)
                visit(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                visit(child)
        elif isinstance(node, str):
            handles.update(re.findall(r"\bP\d{4,}\b", node))

    visit(value)
    return handles, paper_ids


def _chapter_review_source_materials(
    packet: Mapping[str, Any],
    *,
    include_deep_read: bool = False,
) -> list[dict[str, Any]]:
    """Compact only the A/B material for sources cited by this chapter.

    The whole-plan coordinator gets the compact shape (no deep reads): its job
    is cross-chapter placement, not re-reading every paper.  A chapter owner's
    revision call passes ``include_deep_read=True`` so late paid readings are
    reviewed together with the plan they must correct.
    """
    raw_materials = [item for item in packet.get("source_materials") or [] if isinstance(item, Mapping)]
    handles, paper_ids = _source_keys_in_value(packet.get("chapter_plan") or {})
    by_paper = {
        _text((item.get("paper_id"))): _text(item.get("source_handle"))
        for item in raw_materials
        if _text(item.get("paper_id")) and _text(item.get("source_handle"))
    }
    available_handles = {_text(item.get("source_handle")) for item in raw_materials if _text(item.get("source_handle"))}
    used_handles = handles | {by_paper[paper_id] for paper_id in paper_ids if paper_id in by_paper}
    used_handles.update(paper_id for paper_id in paper_ids if paper_id in available_handles)
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_materials:
        handle = _text(item.get("source_handle"))
        if not handle or handle not in used_handles or handle in seen:
            continue
        seen.add(handle)
        row = {
            key: item[key]
            for key in (
                "source_handle", "paper_id", "title", "doi", "year", "material_depth",
                "study_summary_A", "review_planning_B", "supplement_gap_material",
                "supplement_gap_materials", "supplement_material", "supplement_materials",
                "local_passages", "tool_materials", "tool_supplement_materials", "material_identity_conflict",
            )
            if key in item
        }
        if include_deep_read and item.get("deep_read_material"):
            row["deep_read_material"] = _compact_reading_material(item.get("deep_read_material"))
        output.append(row)
    return output


def merge_tool_materials_into_packets(
    packets: Sequence[Mapping[str, Any]],
    tool_materials: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Attach new tool-returned material to the writer packets that should use it.

    Work order 05: material found by the shared queue has to reach the writing
    input, not just the planner's own summary.  A material item carries the
    chapter and unit it was gathered for; the packet keeps every item under the
    program-managed handle so the writer can cite it, and the packet records
    which questions are still open.

    Work order review-v2/01: an item attaches to a single source only when its
    COMPLETE source set is one distinct identity that resolves to a handle
    present in the chapter's own ``source_materials``; it is then merged into
    that source's ``tool_supplement_materials`` so the writing side consumes it
    through the per-source catalog instead of an unread packet top-level list.
    Items citing further sources that never resolved to a handle (paper_id or
    DOI only), several sources, or an unknown handle stay at the chapter level
    with their source set intact; a multi-source synthesis is never folded
    into one paper's record.  ``need_id`` groups rounds of one information
    need: fully identical duplicates fold, a clear update replaces the earlier
    round, complementary content for the same need survives.
    """

    by_chapter: dict[str, list[dict[str, Any]]] = {}
    unassigned: list[dict[str, Any]] = []
    for raw in tool_materials:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        if isinstance(item.get("chapter_ids"), (list, tuple)):
            targets = [_text(value) for value in item["chapter_ids"] if _text(value)]
        else:
            targets = [_text(item.get("chapter_id"))] if _text(item.get("chapter_id")) else []
        for target in targets:
            by_chapter.setdefault(target, []).append(item)
        if not targets:
            unassigned.append(item)

    def item_handles(item: Mapping[str, Any]) -> list[str]:
        handles: list[str] = []
        for source in item.get("sources") or ():
            if isinstance(source, Mapping):
                handle = _text(source.get("source_handle"))
                if handle and handle not in handles:
                    handles.append(handle)
        return handles

    merged: list[dict[str, Any]] = []
    for packet in packets:
        row = dict(packet)
        chapter = row.get("chapter") if isinstance(row.get("chapter"), Mapping) else {}
        chapter_id = _text(row.get("chapter_id") or chapter.get("chapter_id") or chapter.get("id"))
        additions = resolve_tool_materials({**row,
            "tool_materials": [*by_chapter.get(chapter_id, []), *unassigned]})
        if additions:
            source_rows = [
                dict(item) if isinstance(item, Mapping) else item
                for item in (row.get("source_materials") or [])
            ]
            source_by_handle = {
                _text(item.get("source_handle")): item
                for item in source_rows
                if isinstance(item, Mapping) and _text(item.get("source_handle"))
            }
            existing = [item for item in (row.get("tool_materials") or []) if isinstance(item, Mapping)]
            seen = {key for item in existing for key in _tool_material_keys(item)}
            attached_any = False
            for item in additions:
                handles = item_handles(item)
                identities = distinct_tool_material_sources(item)
                if len(identities) == 1 and len(handles) == 1 and handles[0] in source_by_handle and not _text(item.get("unit_key")):
                    target = source_by_handle[handles[0]]
                    target["tool_supplement_materials"] = merge_tool_supplement_entry(
                        target.get("tool_supplement_materials") or [], tool_supplement_entry(item))
                    attached_any = True
                    continue
                keys = _tool_material_keys(item)
                if all(key in seen for key in keys):
                    continue
                seen.update(keys)
                existing.append(item)
            if attached_any or existing != list(row.get("tool_materials") or []):
                row["source_materials"] = source_rows
                row["tool_materials"] = [dict(item) for item in existing]
                row["tool_material_count"] = len(existing)
                row["open_questions"] = list(dict.fromkeys(
                    [*(row.get("open_questions") or []),
                     *[_text(entry.get("still_missing"))
                       for source in source_rows if isinstance(source, Mapping)
                       for entry in (source.get("tool_supplement_materials") or [])
                       if isinstance(entry, Mapping) and _text(entry.get("still_missing"))],
                     *[_text(item.get("still_missing")) for item in existing if _text(item.get("still_missing"))]]
                ))
        merged.append(row)
    return merged


def _render_json_block(value: Any) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2, default=_json_default) + "\n```"


def render_writer_packet_markdown(packet: Mapping[str, Any]) -> str:
    chapter = packet.get("chapter") if isinstance(packet.get("chapter"), Mapping) else {}
    plan = packet.get("chapter_plan") if isinstance(packet.get("chapter_plan"), Mapping) else {}
    lines = [f"# {_text(chapter.get('title') or chapter.get('chapter_id'))}", "", f"**Review question:** {_text(packet.get('research_question'))}", "", "## Placement in the shared outline", ""]
    lines.extend(_render_readable(packet.get("shared_outline"), level=3, label="Shared outline"))
    lines.extend(_render_readable(packet.get("adjacent_chapters"), level=3, label="Adjacent chapters"))
    lines.extend(["", "## Writer-ready chapter plan", ""])
    lines.extend(_render_readable(plan, level=3))
    lines.extend(["", "## Supplied source material", ""])
    for row in packet.get("source_materials") or []:
        if not isinstance(row, Mapping):
            continue
        title = _text(row.get("title") or row.get("paper_id"))
        lines.extend([f"### {title}", "", f"- Source handle: `{_text(row.get('source_handle'))}`", f"- Paper ID: `{_text(row.get('paper_id'))}`"])
        if row.get("doi"):
            lines.append(f"- DOI: {_text(row.get('doi'))}")
        for key, label in (
            ("material_depth", "Available material depth"),
            ("study_summary_A", "Paper understanding from A"),
            ("review_planning_B", "Review-planning account from B"),
            ("supplement_gap_material", "Supplement material"),
            ("supplement_gap_materials", "Other supplement material"),
            ("local_passages", "Focused local passages"),
            ("deep_read_material", "Directed-reading material"),
        ):
            if row.get(key):
                lines.append("")
                lines.extend(_render_readable(row[key], level=4, label=label))
        lines.append("")
    lines.extend(["## Citation and scope rules", ""])
    lines.extend(_render_readable(packet.get("citation_namespace_rule") or packet.get("writer_handoff") or {}, level=3))
    return "\n".join(lines).rstrip() + "\n"


_FIELD_LABELS_ZH = {
    "title": "标题", "purpose": "本节作用", "scope": "范围", "thesis": "本章主张",
    "reader_objective": "读者应理解", "units": "论述单元", "subsections": "子节",
    "sections": "章节", "substantive_threads": "论证线索", "point": "核心观点",
    "substantive_point": "核心观点", "ordered_development": "展开顺序",
    "development": "展开方式", "cases": "具体研究与案例", "case": "案例",
    "paper_references": "论文依据", "references": "参考材料", "cross_paper_synthesis": "跨论文综合",
    "synthesis": "跨论文综合", "conditions": "适用条件", "evidence_conditions": "证据条件",
    "limitations": "边界与限制", "limits": "边界与限制", "transition": "承接下一节",
    "transitions": "章节衔接", "chapter_id": "章节编号", "review_title": "综述标题",
    "central_question": "中心问题", "central_thesis": "中心论点", "central_argument": "中心论证",
    "review_argument": "综述论证", "shared_scope": "共同范围", "shared_outline": "共同提纲",
    "material_theme_inventory": "材料主题", "unresolved_limits": "尚存限制",
    "source_handles": "来源", "source_handle": "来源编号", "paper_id": "论文编号",
    "bibliography": "文献信息", "finding": "研究发现", "method": "方法",
    "population": "研究对象", "model": "研究模型", "outcome": "研究结果",
    "comparison": "比较", "evidence_type": "证据类型", "review_reported": "综述转述的原始研究",
    "reason": "说明", "notes": "说明", "harmonization_notes": "统筹说明",
}


def _readable_label(key: str) -> str:
    if key in _FIELD_LABELS_ZH:
        return _FIELD_LABELS_ZH[key]
    return re.sub(r"[_-]+", " ", key).strip().capitalize()


def _render_readable(value: Any, *, level: int = 3, label: str = "") -> list[str]:
    """Render nested plan fields as headings and prose instead of JSON dumps."""
    if value is None or value == "" or value == [] or value == {}:
        return []
    label_text = _readable_label(label) if label else ""
    indent = "  " * max(0, level - 3)
    if isinstance(value, Mapping):
        data = dict(value)
        own_title = _text(data.get("title") or data.get("name") or data.get("label"))
        lines: list[str] = []
        if own_title:
            lines.append("#" * min(6, max(3, level)) + " " + own_title)
        elif label_text:
            lines.append("#" * min(6, max(3, level)) + " " + label_text)
        elif level == 3:
            lines.append("### 规划内容")
        for key, item in data.items():
            if key in {"title", "name", "label"} or item is None or item == "" or item == [] or item == {}:
                continue
            rendered = _render_readable(item, level=min(6, level + 1), label=str(key))
            lines.extend(rendered)
        return lines
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = list(value)
        if not items:
            return []
        lines: list[str] = []
        if label_text:
            lines.append("#" * min(6, max(3, level)) + " " + label_text)
        for index, item in enumerate(items, start=1):
            if isinstance(item, Mapping):
                title = _text(item.get("title") or item.get("name") or item.get("unit_title") or item.get("chapter_id"))
                if title:
                    lines.append(f"{indent}- **{title}**")
                    body = {key: child for key, child in item.items() if key not in {"title", "name", "unit_title", "chapter_id"} and child not in (None, "", [], {})}
                    for key, child in body.items():
                        lines.extend(_render_readable(child, level=min(6, level + 1), label=str(key)))
                else:
                    lines.append(f"{indent}- **{index}.**")
                    lines.extend(_render_readable(item, level=min(6, level + 1)))
            elif isinstance(item, (list, tuple)):
                lines.extend(_render_readable(item, level=min(6, level + 1)))
            else:
                lines.append(f"{indent}- {_text(item)}")
        return lines
    text = _text(value)
    if not text:
        return []
    if label_text:
        return [f"{indent}- **{label_text}:** {text}"]
    return [f"{indent}{text}"]


def render_plan_markdown(plan: Mapping[str, Any]) -> str:
    provisional = plan.get("provisional_scope") if isinstance(plan.get("provisional_scope"), Mapping) else {}
    title = _text(plan.get("review_title") or provisional.get("review_title") or plan.get("research_question") or "综述规划")
    lines = [f"# {title}", "", f"**研究问题：** {_text(plan.get('research_question'))}", f"**状态：** {_text(plan.get('status'))}", f"**规划阶段：** {_text(plan.get('completed_through') or '详细规划')}", ""]
    scope = plan.get("shared_scope") or plan.get("provisional_scope") or {}
    lines.extend(["## 综述范围与论证主线", ""])
    lines.extend(_render_readable(scope, level=3))
    lines.extend(["", "## 共同提纲", ""])
    outline = plan.get("shared_outline") or {}
    lines.extend(_render_readable(outline, level=3))
    lines.extend(["", "## 章节写作计划", ""])
    for row in plan.get("chapters") or []:
        if not isinstance(row, Mapping):
            continue
        chapter = row.get("chapter") if isinstance(row.get("chapter"), Mapping) else {}
        chapter_title = _text(chapter.get("title") or chapter.get("chapter_id") or "章节")
        lines.extend([f"### {chapter_title}", ""])
        for key in ("purpose", "scope", "substantive_threads"):
            if chapter.get(key):
                lines.extend(_render_readable(chapter[key], level=4, label=key))
        lines.extend(_render_readable(row.get("chapter_plan"), level=4, label="chapter_plan"))
        lines.append("")
    lines.extend(["## 写作材料包", ""])
    for row in plan.get("writer_packets") or []:
        if isinstance(row, Mapping):
            lines.append(f"- **{_text(row.get('chapter_id'))}:** {_text(row.get('source_material_count'))} 篇已整理来源；文件：`{_text(row.get('markdown_path'))}`")
    if plan.get("candidate_screening"):
        screening = plan["candidate_screening"]
        lines.extend(["", "## 材料覆盖", "", f"已读取完整 B 池，共 {screening.get('pool_rows_read', 0)} 条；其中 {screening.get('unique_sources_assigned', 0)} 篇至少分配到一章。未分配材料仍保留在 JSON 筛选记录中，文献数量不作为硬性门槛。"])
    if plan.get("whole_plan_improvement"):
        lines.extend(["", "## 全篇统筹调整", ""])
        lines.extend(_render_readable(plan["whole_plan_improvement"], level=3))
    if plan.get("deep_read_budget"):
        lines.extend(["", "## 定向精读", ""])
        lines.extend(_render_readable(plan["deep_read_budget"], level=3))
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "SCHEMA_VERSION", "ProgressivePlanError", "ProgressivePlannerConfig", "ProgressiveReviewPlanner",
    "QwenProgressivePlanner", "load_original_plan", "load_planning_pool", "load_prior_readings", "qwen_local_token_counter",
    "make_planning_supplement_runner", "make_retrieval_loop_runner", "make_directed_reading_runner",
    "render_plan_markdown", "render_writer_packet_markdown",
]
