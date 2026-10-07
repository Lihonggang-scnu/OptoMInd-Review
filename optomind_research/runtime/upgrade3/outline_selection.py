"""Autonomous unit selection for opt-in outline strengthening.

This layer chooses worthwhile outline work and groups it.  It never rewrites a
plan or supplies scientific answers.  The existing on-demand owner path then
receives one chapter-local projection for each selected same-chapter group.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import outline_strengthening as strengthening
from . import outline_on_demand as on_demand
from . import progressive_review_plan as planning


SELECTION_SCHEMA = "optomind.outline_unit_selection.v2"
SELECTION_ROLE = "outline_selection"
_FORBIDDEN_TOP_LEVEL = frozenset({
    "review_targets", "expected_answers", "prior_candidate", "edited_body",
    "replacement_text", "chapter_feedback", "editorial_feedback_for_chapter",
})
_NO_SELECTION_STATUSES = frozenset({"none", "no_change", "unchanged", "no-change"})
_MODEL_STRIP_KEYS = frozenset({"card_path", "excluded_source_ids", "excluded_source_handles", "source_exclusion_notes"})


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _ids(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _unit_id(unit: Mapping[str, Any]) -> str:
    return str(unit.get("unit_id") or unit.get("id") or "").strip()


def _chapter_id(row: Mapping[str, Any]) -> str:
    return str(row.get("chapter_id") or row.get("chapter", {}).get("chapter_id") or "").strip()


def _unit_rows(chapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    plan = chapter.get("chapter_plan") if isinstance(chapter.get("chapter_plan"), Mapping) else {}
    rows = plan.get("units") if isinstance(plan, Mapping) else []
    return [dict(row) for row in rows or [] if isinstance(row, Mapping)]


def _material_index(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Expose identity and readable locators without duplicating full material cards."""

    channels = {}
    for channel in ("source_materials", "candidate_materials", "tool_materials"):
        rows = []
        for row in payload.get(channel) or []:
            if not isinstance(row, Mapping):
                continue
            identity_keys = (
                "source_handle", "paper_id", "original_paper_id", "original_source_handle",
                "title", "doi", "study_id",
            )
            item = {key: _copy(row[key]) for key in identity_keys if key in row}
            paths = row.get("available_material_paths") or row.get("material_paths") or row.get("available_paths")
            if isinstance(paths, Sequence) and not isinstance(paths, (str, bytes)):
                item["available_material_paths"] = [str(path) for path in paths]
            elif isinstance(row.get("source_supplied_locators"), Sequence):
                item["available_material_paths"] = [
                    str(locator.get("path")) for locator in row.get("source_supplied_locators") or []
                    if isinstance(locator, Mapping) and str(locator.get("path") or "").strip()
                ]
            for key in ("research_scope", "topic", "study_type", "source_description"):
                value = row.get(key)
                if isinstance(value, str) and value.strip():
                    item[key] = value
            item["full_record_available_for_owner"] = True
            rows.append(item)
        channels[channel] = rows
    return channels


_SELECTOR_SEMANTIC_FIELDS = frozenset({
    "problem_or_question", "research_question", "question", "research_scope", "scope",
    "key_findings", "key_results", "finding", "findings", "claim", "result", "results",
    "contribution_and_limits", "conditions", "boundary_conditions", "limits", "limitations",
    "scope_interpretation_cautions", "planning_summary", "reported_studies", "evidence",
    "observations", "mechanism", "mechanisms", "counterexamples", "usable_content",
    "useful_material", "auxiliary_material", "current_question_material", "question_material",
    "prior_question_material", "remaining_gap", "still_missing", "allowed_use", "source_role",
    "intended_use",
})


def _selector_semantic_locator(value: Any, path: str) -> dict[str, Any]:
    """A source-field directory, not a second full-material reader.

    Keep every record in selected finding/condition/limit fields. Other fields
    stay explicitly requestable; no prefix or list-item quota is applied here.
    Unknown material shapes retain the existing semantic catalog policy.
    """
    if isinstance(value, Mapping):
        selected = {key: child for key, child in value.items()
                    if key in _SELECTOR_SEMANTIC_FIELDS
                    or "condition" in str(key).casefold() or "limit" in str(key).casefold()}
        if selected:
            omitted = [f"{path}.{key}" for key in value if key not in selected]
            return {"path": path, "source_excerpt": json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    "excerpt": bool(omitted), "omitted_material_paths": omitted,
                    "omitted_materials_requestable": True, "selection": "complete_semantic_fields"}
    return on_demand._semantic_locator(value, path)


def _semantic_material_catalog(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Reuse the owner catalog, including nested and review-mediated records.

    Top-level conditions/limits are source-authored qualifiers too. Expose them
    through the same whole-field locator policy, without requiring an A/B pair.
    No new model pass, prefix clipping, or replacement evidence is involved.
    """
    catalog_input = payload
    if not isinstance(payload.get("autonomous_outline_strengthening"), Mapping):
        # Plain chapter packets were always accepted by the selector. Attach
        # only the catalog's read-only envelope to a copy; preserve all fields
        # and any supplied integrity assertion rather than masking corruption.
        catalog_input = _copy(payload)
        catalog_input["autonomous_outline_strengthening"] = {"enabled": True}
    catalog = on_demand.build_material_catalog(catalog_input)
    public = on_demand.public_catalog(catalog)
    public["locator_policy"] = {"version": "selector_complete_semantic_fields_v2", "whole_fields_no_prefix": True, "omitted_materials_requestable": True}
    for entry in public["entries"]:
        record = catalog["_lookup"][entry["access_id"]]["record"]
        entry["source_supplied_locators"] = [
            _selector_semantic_locator(record[locator["path"]], locator["path"])
            for locator in entry["source_supplied_locators"]
        ]
        existing = {row["path"] for row in entry["source_supplied_locators"]}
        supplemental_paths = [key for key in record if key in _SELECTOR_SEMANTIC_FIELDS
                              or "condition" in str(key).casefold() or "limit" in str(key).casefold()]
        entry["available_material_paths"] = list(dict.fromkeys([*entry["available_material_paths"], *supplemental_paths]))
        for path in entry["available_material_paths"]:
            if path in record and path not in existing:
                entry["source_supplied_locators"].append({
                    "path": path, "source_excerpt": json.dumps(record[path], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    "excerpt": False, "selection": "complete_semantic_field",
                })
    public["catalog_sha256"] = _hash({"entries": public["entries"], "locator_policy": public["locator_policy"]})
    return public


def build_selection_payload(
    chapter_payloads: Sequence[Mapping[str, Any]], *,
    research_question: str = "", shared_outline: Any = None, shared_scope: Mapping[str, Any] | None = None,
    review_argument: Any = "", review_argument_status: str = "", review_argument_source: str = "",
    actual_local_body_by_chapter: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build a complete plan and semantic-directory selector input from chapter packets.

    Chapter plans are copied in full.  Body text is optional and, when supplied,
    is copied in full per chapter; no character-prefix projection is used.
    Full source records remain in the chapter payloads for the downstream owner.
    Source-authored semantic locators expose findings, conditions and limits.
    """

    chapters = []
    seen_units: set[str] = set()
    for raw in chapter_payloads:
        if not isinstance(raw, Mapping):
            raise ValueError("selection_chapter_payload_must_be_object")
        chapter_id = _chapter_id(raw)
        if not chapter_id:
            raise ValueError("selection_chapter_id_required")
        rows = _unit_rows(raw)
        if not rows:
            raise ValueError(f"selection_chapter_units_missing:{chapter_id}")
        current_ids = [_unit_id(row) for row in rows]
        if any(not value for value in current_ids):
            raise ValueError(f"selection_unit_id_missing:{chapter_id}")
        overlap = seen_units & set(current_ids)
        if overlap:
            raise ValueError("selection_unit_id_duplicate:" + ",".join(sorted(overlap)))
        seen_units.update(current_ids)
        context = _copy(raw.get("full_chapter_context") or {})
        chapter = _copy(raw.get("chapter") or {})
        body_map = actual_local_body_by_chapter or {}
        body = body_map.get(chapter_id, raw.get("actual_local_body") or "")
        chapters.append({
            "chapter_id": chapter_id,
            "chapter": chapter,
            "chapter_plan": _copy(raw.get("chapter_plan") or {}),
            "modifiable_unit_ids": _ids(raw.get("modifiable_unit_ids") or current_ids),
            "read_only_unit_ids": _ids(raw.get("read_only_unit_ids") or []),
            "readonly_neighbor_unit_roles": _copy(raw.get("readonly_neighbor_unit_roles") or []),
            "full_chapter_context": context,
            "actual_local_body": str(body or ""),
            "material_index": _material_index(raw),
            "material_catalog": _semantic_material_catalog(raw),
            "source_identity_map": _copy(raw.get("source_identity_map") or {}),
        })
    if not chapters:
        raise ValueError("selection_chapters_required")
    declared_forbidden = sorted({
        unit_id
        for chapter in chapters
        for unit_id in (
            (
                set(_ids(chapter.get("read_only_unit_ids") or [])) - seen_units
            )
            | (
                set(_unit_id(row) for row in _unit_rows(chapter))
                - set(_ids(chapter.get("modifiable_unit_ids") or []))
            )
        )
    })
    payload = {
        "schema_version": SELECTION_SCHEMA,
        "selection_mode": {
            "enabled": True,
            "input_mode": "autonomous_from_full_outline",
            "allow_none": True,
            "allow_cross_chapter_groups": False,
            "editable_scope": "same_chapter_local_cluster",
            "global_and_neighbor_context": "read_only",
            "selection_objective": "necessary_high_benefit_local_underdevelopment",
            "rewrite_plan": False,
            "do_not_add_evidence": True,
        },
        "research_question": str(research_question or ""),
        "shared_outline": _copy(shared_outline or []),
        "shared_scope": _copy(shared_scope or {}),
        "review_argument": _copy(review_argument),
        "review_argument_status": str(review_argument_status or ""),
        "review_argument_source": str(review_argument_source or ""),
        "chapters": chapters,
        "all_unit_ids": sorted(seen_units),
        "selector_forbidden_unit_ids": declared_forbidden,
        "call_id": "outline-unit-selection",
    }
    _verify_selection_payload(payload)
    return payload


def _compact_runtime_metadata(value: Any) -> Any:
    """Remove only transport/runtime metadata outside the lossless plans."""

    if isinstance(value, Mapping):
        return {
            str(key): _compact_runtime_metadata(child)
            for key, child in value.items()
            if str(key) not in _MODEL_STRIP_KEYS
        }
    if isinstance(value, list):
        return [_compact_runtime_metadata(child) for child in value]
    return _copy(value)


def _referenced_material_entries(chapter: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Navigate only identities explicitly present in the complete local plan.

    Match stable identifiers, never topic keywords, titles or an inferred
    relevance ranking. Unreferenced records remain in the owner's full packet.
    """
    mentioned: set[str] = set()
    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                mentioned.add(str(key))
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            mentioned.add(value.strip())
    visit(chapter.get("chapter_plan") or {})
    entries = (chapter.get("material_catalog") or {}).get("entries") or []
    return [entry for entry in entries if any(
        str((entry.get("identity") or {}).get(key) or "").strip() in mentioned
        for key in ("source_handle", "paper_id", "original_paper_id", "original_source_handle", "study_id", "doi")
        if (entry.get("identity") or {}).get(key)
    )]


def _navigation_catalog(chapters: Sequence[Mapping[str, Any]]) -> tuple[dict, dict, str]:
    """Group navigation, never scientific content, by exact source identity."""
    catalog: dict[str, dict[str, Any]] = {}
    chapter_keys: dict[str, set[str]] = {}
    inventory = []
    for chapter in chapters:
        chapter_id = _chapter_id(chapter)
        if not isinstance(chapter.get("material_catalog"), Mapping):
            raise ValueError(f"selection_semantic_catalog_required:{chapter_id}:rebuild_from_original_chapter_packets")
        chapter_keys[chapter_id] = set()
        for raw in _referenced_material_entries(chapter):
            identity = raw.get("identity") or {}
            key = _hash(identity)
            display = {field: _copy(identity[field]) for field in
                       ("source_handle", "paper_id", "original_paper_id", "original_source_handle", "study_id")
                       if identity.get(field)}
            if not display:
                display = _copy(identity)
            entry = catalog.setdefault(key, {
                "identity": display, "available_material_paths": [],
                "source_supplied_locators": [], "readback_locations": [],
            })
            roots = {str(path).split(".")[0] for path in raw.get("available_material_paths") or []}
            entry["available_material_paths"] = sorted(set(entry["available_material_paths"]) | roots)
            location = [chapter_id, str(raw.get("access_id") or raw.get("index_path") or "")]
            if location not in entry["readback_locations"]:
                entry["readback_locations"].append(location)
            inventory.append([key, *location, raw.get("record_sha256")])
            chapter_keys[chapter_id].add(key)
    # When minimal identities collide, retain full identity to disambiguate.
    displays: dict[str, list[str]] = {}
    for key, entry in catalog.items():
        displays.setdefault(_hash(entry["identity"]), []).append(key)
    full_identities = {_hash(raw.get("identity") or {}): raw.get("identity") or {}
                       for chapter in chapters for raw in _referenced_material_entries(chapter)}
    for keys in displays.values():
        if len(keys) > 1:
            for key in keys:
                catalog[key]["identity"] = _copy(full_identities[key])
    for index, key in enumerate(sorted(catalog)):
        catalog[key]["identity_key"] = f"nav-{index}"
    return catalog, chapter_keys, _hash(inventory)


def model_visible_selection_payload(
    payload: Mapping[str, Any], *, include_material_index: bool = False,
) -> dict[str, Any]:
    """Project a compact selector view without truncating any chapter plan.

    The complete payload remains the local source of truth for owner requests.
    This view removes absolute card paths, exclusion inventories, repeated
    neighbour prose, and per-chapter duplicate identity maps.  It retains a
    referenced-source navigation by default; complete semantic locators are an
    explicit diagnostic option. Both retain chapter-qualified readback locations.
    """

    _verify_selection_payload(payload)
    catalog: dict[str, dict[str, Any]] = {}
    chapter_material_keys: dict[str, set[str]] = {}
    original_chapters = payload.get("chapters") or []
    for chapter in original_chapters:
        chapter_id = _chapter_id(chapter)
        source_catalog = chapter.get("material_catalog")
        if not isinstance(source_catalog, Mapping):
            raise ValueError(f"selection_semantic_catalog_required:{chapter_id}:rebuild_from_original_chapter_packets")
        material_keys: set[str] = set()
        entries = (source_catalog.get("entries") or []) if include_material_index else _referenced_material_entries(chapter)
        for raw in entries:
            # Identity alone is not sufficient: chapter-specific evidence
            # variants must remain distinct even under an identical handle.
            key = _hash({"identity": raw.get("identity"), "record_sha256": raw.get("record_sha256")})
            entry = catalog.setdefault(key, {
                "identity_key": key,
                "identity": _copy(raw.get("identity") or {}),
                "record_sha256": raw.get("record_sha256"),
                "available_material_paths": _copy(raw.get("available_material_paths") or []) if include_material_index else sorted({str(path).split(".")[0] for path in raw.get("available_material_paths") or []}),
                "source_supplied_locators": _copy(raw.get("source_supplied_locators") or []) if include_material_index else [],
                "readback_locations": [],
                "full_record_available_for_owner": True,
            })
            location = {"chapter_id": chapter_id, "channel": raw.get("channel"),
                        "access_id": raw.get("access_id"), "index_path": raw.get("index_path")}
            if location not in entry["readback_locations"]:
                entry["readback_locations"].append(location)
            material_keys.add(key)
        chapter_material_keys[chapter_id] = material_keys
    inventory_fingerprint = ""
    if not include_material_index:
        catalog, chapter_material_keys, inventory_fingerprint = _navigation_catalog(original_chapters)
    material_catalog = [catalog[key] for key in sorted(catalog)]
    catalog_index = {key: index for index, key in enumerate(sorted(catalog))}
    shared_contents: list[str] = []
    content_indexes: dict[str, int] = {}
    shared_paths: list[list[str]] = []
    path_indexes: dict[str, int] = {}
    for entry in material_catalog:
        paths = entry.pop("available_material_paths")
        path_key = _hash(paths)
        if path_key not in path_indexes:
            path_indexes[path_key] = len(shared_paths)
            shared_paths.append(paths)
        entry["available_material_paths_ref"] = f"#/shared_material_paths/{path_indexes[path_key]}"
        for locator in entry["source_supplied_locators"]:
            text = locator.pop("source_excerpt")
            if text not in content_indexes:
                content_indexes[text] = len(shared_contents)
                shared_contents.append(text)
            locator["source_excerpt_ref"] = f"#/shared_material_contents/{content_indexes[text]}"

    chapters = []
    for chapter in original_chapters:
        chapter_id = _chapter_id(chapter)
        chapter_raw = _compact_runtime_metadata(chapter.get("chapter") or {})
        chapter_view = {
            key: _copy(chapter_raw[key])
            for key in ("chapter_id", "title", "purpose", "scope", "coordinated_scope", "reader_objective")
            if key in chapter_raw
        }
        role_ids = _ids([
            row.get("unit_id") for row in (chapter.get("readonly_neighbor_unit_roles") or [])
            if isinstance(row, Mapping)
        ])
        role_chapters = sorted({str(row.get("chapter_id") or "") for row in (chapter.get("readonly_neighbor_unit_roles") or []) if isinstance(row, Mapping) and str(row.get("chapter_id") or "").strip()})
        chapters.append({
            "chapter_id": chapter_id,
            "chapter": chapter_view,
            # Exact equality is intentional: this layer must not trim science
            # or writing duties from any unit.
            "chapter_plan": _copy(chapter.get("chapter_plan") or {}),
            "modifiable_unit_ids": _ids(chapter.get("modifiable_unit_ids") or []),
            "read_only_unit_ids": _ids(chapter.get("read_only_unit_ids") or []),
            "readonly_neighbor_unit_ids": role_ids,
            "readonly_neighbor_chapter_ids": role_chapters,
            "full_chapter_context": _compact_runtime_metadata(chapter.get("full_chapter_context") or {}),
            "actual_local_body": str(chapter.get("actual_local_body") or ""),
            "material_identity_refs": [f"#/material_identity_catalog/{catalog_index[key]}" for key in sorted(chapter_material_keys.get(chapter_id) or [])],
        })
    projected = {
        "schema_version": SELECTION_SCHEMA,
        "selection_mode": _copy(payload.get("selection_mode") or {}),
        "research_question": str(payload.get("research_question") or ""),
        "shared_outline": _compact_runtime_metadata(payload.get("shared_outline") or []),
        "shared_scope": _compact_runtime_metadata(payload.get("shared_scope") or {}),
        "review_argument": _copy(payload.get("review_argument") or ""),
        "review_argument_status": str(payload.get("review_argument_status") or ""),
        "review_argument_source": str(payload.get("review_argument_source") or ""),
        "chapters": chapters,
        "all_unit_ids": _ids(payload.get("all_unit_ids") or []),
        "selector_forbidden_unit_ids": _ids(payload.get("selector_forbidden_unit_ids") or []),
        "call_id": str(payload.get("call_id") or "outline-unit-selection"),
        "model_visible_projection": {
            "source_payload_preserved_locally": True,
            "chapter_plan_lossless": True,
            "runtime_metadata_compacted": True,
            "neighbor_roles_as_ids_only": True,
            "shared_identity_catalog": True,
            "include_material_index": bool(include_material_index),
            "semantic_material_catalog": bool(include_material_index),
            "outline_first": not include_material_index,
            "material_visibility": "full_semantic_diagnostic" if include_material_index else "plan_referenced_navigation_only",
            "warning": "" if include_material_index else "Material content is not shown; selection gaps are hypotheses for the owner to verify against complete source records",
        },
    }
    projected["material_identity_catalog"] = material_catalog
    projected["shared_material_paths"] = shared_paths
    if not include_material_index:
        projected["material_inventory_fingerprint"] = inventory_fingerprint
        projected["model_visible_projection"]["readback_location_fields"] = ["chapter_id", "access_id"]
        projected["model_visible_projection"]["full_records_available_for_owner"] = True
    if include_material_index:
        projected["shared_material_contents"] = shared_contents
    verify_model_visible_projection(payload, projected)
    return projected


def _pointer_target(root: Mapping[str, Any], pointer: str) -> Any:
    if not pointer.startswith("#/"):
        raise ValueError("selection_material_pointer_invalid")
    value: Any = root
    for part in pointer[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            value = value[int(part)]
        elif isinstance(value, Mapping):
            value = value[part]
        else:
            raise ValueError("selection_material_pointer_unresolvable")
    return value


def verify_model_visible_projection(original: Mapping[str, Any], projected: Mapping[str, Any]) -> dict[str, Any]:
    """Check complete plan equality and every shared-catalog pointer."""

    _verify_selection_payload(original)
    _verify_selection_payload(projected)
    originals = {str(chapter["chapter_id"]): chapter for chapter in original.get("chapters") or []}
    visibles = {str(chapter["chapter_id"]): chapter for chapter in projected.get("chapters") or []}
    if set(originals) != set(visibles):
        raise ValueError("selection_projection_chapters_changed")
    outline_first = bool(projected.get("model_visible_projection", {}).get("outline_first"))
    if outline_first:
        expected_catalog, expected_chapter_keys, fingerprint = _navigation_catalog(original.get("chapters") or [])
        if projected.get("material_inventory_fingerprint") != fingerprint:
            raise ValueError("selection_projection_inventory_fingerprint_changed")
        expected_navigation = {entry["identity_key"]: entry for entry in expected_catalog.values()}
        visible_navigation = {entry.get("identity_key"): entry for entry in projected.get("material_identity_catalog") or []}
        if set(expected_navigation) != set(visible_navigation):
            raise ValueError("selection_projection_navigation_coverage_changed")
        for key, entry in expected_navigation.items():
            actual = _copy(visible_navigation[key])
            ref = actual.pop("available_material_paths_ref", None)
            if ref is not None:
                if "available_material_paths" in actual:
                    raise ValueError("selection_projection_ambiguous_paths")
                actual["available_material_paths"] = _pointer_target(projected, str(ref))
            if actual != entry:
                raise ValueError("selection_projection_navigation_changed")
    for chapter_id, source in originals.items():
        visible = visibles[chapter_id]
        if visible.get("chapter_plan") != source.get("chapter_plan"):
            raise ValueError(f"selection_projection_plan_changed:{chapter_id}")
        for key in ("card_path", "excluded_source_ids", "excluded_source_handles", "source_exclusion_notes"):
            if key in visible.get("chapter") or key in visible.get("full_chapter_context"):
                raise ValueError(f"selection_projection_forbidden_key:{chapter_id}:{key}")
        for field in ("modifiable_unit_ids", "read_only_unit_ids"):
            if visible.get(field) != source.get(field):
                raise ValueError(f"selection_projection_scope_changed:{chapter_id}:{field}")
        actual_materials = {}
        for pointer in visible.get("material_identity_refs") or []:
            target = _pointer_target(projected, str(pointer))
            if not isinstance(target, Mapping) or not target.get("identity_key"):
                raise ValueError(f"selection_projection_pointer_invalid:{chapter_id}")
            actual_materials[target["identity_key"]] = target
        semantic = bool(projected.get("model_visible_projection", {}).get("semantic_material_catalog"))
        if outline_first:
            expected_keys = {expected_catalog[key]["identity_key"] for key in expected_chapter_keys[chapter_id]}
            if set(actual_materials) != expected_keys:
                raise ValueError(f"selection_projection_material_coverage_changed:{chapter_id}")
        if semantic:
            source_catalog = source.get("material_catalog")
            if not isinstance(source_catalog, Mapping):
                raise ValueError(f"selection_semantic_catalog_required:{chapter_id}:rebuild_from_original_chapter_packets")
            expected_keys = set()
            entries = (source_catalog.get("entries") or []) if semantic else _referenced_material_entries(source)
            for entry in entries:
                key = _hash({"identity": entry.get("identity"), "record_sha256": entry.get("record_sha256")})
                expected_keys.add(key)
                target = actual_materials.get(key) or {}
                for field in ("identity", "record_sha256", "available_material_paths", "source_supplied_locators"):
                    actual = target.get(field)
                    if field == "available_material_paths" and target.get("available_material_paths_ref"):
                        if "available_material_paths" in target:
                            raise ValueError(f"selection_projection_ambiguous_paths:{chapter_id}")
                        actual = _pointer_target(projected, str(target["available_material_paths_ref"]))
                    if field == "source_supplied_locators" and isinstance(actual, list):
                        actual = _copy(actual)
                        for locator in actual:
                            ref = locator.pop("source_excerpt_ref", None)
                            if ref is not None:
                                if "source_excerpt" in locator:
                                    raise ValueError(f"selection_projection_ambiguous_excerpt:{chapter_id}")
                                locator["source_excerpt"] = _pointer_target(projected, str(ref))
                    expected = entry.get(field)
                    if not semantic and field == "source_supplied_locators":
                        expected = []
                    elif not semantic and field == "available_material_paths":
                        expected = sorted({str(path).split(".")[0] for path in expected or []})
                    if actual != expected:
                        raise ValueError(f"selection_projection_material_changed:{chapter_id}:{field}")
                location = {"chapter_id": chapter_id, "channel": entry.get("channel"),
                            "access_id": entry.get("access_id"), "index_path": entry.get("index_path")}
                if location not in target.get("readback_locations", []):
                    raise ValueError(f"selection_projection_readback_missing:{chapter_id}")
            if set(actual_materials) != expected_keys:
                raise ValueError(f"selection_projection_material_coverage_changed:{chapter_id}")
    return {
        "chapter_count": len(visibles),
        "unit_count": len(_ids(projected.get("all_unit_ids") or [])),
        "catalog_count": len(projected.get("material_identity_catalog") or []),
        "plan_sha256_by_chapter": {chapter_id: _hash(chapter.get("chapter_plan") or {}) for chapter_id, chapter in originals.items()},
        "pointer_count": sum(len(chapter.get("material_identity_refs") or []) for chapter in visibles.values()),
    }


def _verify_selection_payload(payload: Mapping[str, Any]) -> None:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("selection_mode"), Mapping):
        raise ValueError("selection_payload_required")
    if not payload.get("selection_mode", {}).get("enabled"):
        raise ValueError("selection_mode_not_enabled")
    for key in _FORBIDDEN_TOP_LEVEL:
        if payload.get(key):
            raise ValueError(f"selection_manual_control_forbidden:{key}")
    chapters = payload.get("chapters") or []
    if not isinstance(chapters, list) or not chapters:
        raise ValueError("selection_chapters_required")
    known: set[str] = set()
    for chapter in chapters:
        if not isinstance(chapter, Mapping) or not _chapter_id(chapter):
            raise ValueError("selection_chapter_invalid")
        rows = _unit_rows(chapter)
        ids = [_unit_id(row) for row in rows]
        if len(ids) != len(set(ids)) or any(not value for value in ids):
            raise ValueError(f"selection_unit_ids_invalid:{_chapter_id(chapter)}")
        known.update(ids)
    if set(_ids(payload.get("all_unit_ids"))) != known:
        raise ValueError("selection_all_unit_ids_mismatch")


def selection_messages(
    payload: Mapping[str, Any], *, model_payload: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Build a full-outline, selection-only request with no domain targets."""

    _verify_selection_payload(payload)
    visible = model_visible_selection_payload(payload) if model_payload is None else model_payload
    if model_payload is not None:
        verify_model_visible_projection(payload, model_payload)
        visible = model_payload
    system = (
        "你是综述细纲加强前的自主任务选择器。只选择必要且预期收益高的局部未展开单元；"
        "目标是更好回答用户研究问题及其子问题。以现有完整细纲为主要依据，完整阅读研究问题、全篇职责、每章完整 chapter_plan、"
        "paragraph_briefs、supporting_studies、证据用途、条件限制与论证关系。材料信息只作辅助，不重新审读全材料池。全篇与邻章只用于只读背景。"
        "优先识别材料已经支持但写作任务尚未展开的缺口：机制材料仍停留在笼统介绍而无解释关系；"
        "有研究却没有比较维度或比较意义；决定性条件、反例或限制未进入写作任务；"
        "一个任务混合了需要分别回答的不同问题。还可识别证据到主张之间缺少必要推理或比较步骤的写作任务，"
        "只指出缺失的分析职责，不断言主张错误；或同章局部任务重复同一证据、分析与结论，"
        "没有不同的知识推进且妨碍回答子问题。重复引用本身不是缺口，不做风格性去重。"
        "也可发现同性质的局部展开不足，但必须有输入依据；不得扩成全篇审稿或章节职责重分配。理由应定位任务、缺口依据和所服务子问题的收益。"
        "每个选择都权衡局部修订的边际收益、材料可得性与后续读取/编辑成本；"
        "不要因篇幅短、引用少、格式不齐、术语可润色或过渡句可优化就选择。"
        "现有任务已经充分回答子问题时保留；无足够证据或收益不足可返回 none/no_change；不设数量配额。"
        "每组可编辑 unit_ids 必须属于同一章节，仅组合确实需要一起处理的局部单元，禁止跨章逻辑组；"
        "跨章联系只能放 related_read_only_unit_ids，不能成为跨章可编辑任务。"
        "selection_reason 说明为什么值得投入；improvement_focus 说明当前缺口、材料定位、"
        "应承担的解释/比较/条件任务及局部边界，而不是写出科学答案、正确结论或替换措辞。"
        "你不改写细纲、不提供科学修订答案、不写正文、不补充外部证据。只输出 JSON。"
        "模型可见输入保留完整 chapter_plan；本地绝对卡片位置、运行排除清单和重复邻居正文未发送。"
        "不把定位目录当成已经阅读的证据，局部缺口是交给下层核验的待办假设，不能宣称已确认科学事实。"
        "综述转述、候选、工具及补充材料按实际来源和限制使用，不强制要求每条证据同时有 A/B。"
    )
    if visible.get("model_visible_projection", {}).get("semantic_material_catalog"):
        system += (
            "本次显式附加语义材料诊断目录；source_excerpt_ref 指向请求内 shared_material_contents 原文字段，"
            "available_material_paths_ref 指向 shared_material_paths；省略字段仍可由下层回读。"
        )
    else:
        system += (
            "默认只附加当前细纲明确引用来源的轻量导航，不发送全材料池身份表、A/B摘要或工具答案。"
            "material_identity_refs 是请求内 JSON pointer；available_material_paths_ref 指向可回读材料根字段，"
            "readback_locations 每项为 [chapter_id, access_id]，给出各完整原记录的位置（同身份不同记录仍分别可读）。未列来源不表示不存在；完整材料仅由下层按需读取核验。"
        )
    user_contract = {
        "status": "selected | none | no_change",
        "groups": [{
            "group_id": "model-stable-label",
            "unit_ids": ["existing stable unit IDs"],
            "selection_reason": "material-backed reason to invest in this group",
            "improvement_focus": ["specific organization/evidence/condition/comparison responsibility"],
            "related_read_only_unit_ids": ["existing units that should remain boundary context"],
        }],
    }
    user = (
        "返回且只返回符合以下合同的 JSON：" + json.dumps(user_contract, ensure_ascii=False, separators=(",", ":"))
        + "。status=none 或 no_change 时 groups 必须为空。unit_ids 必须来自输入且不得重复；"
        "related_read_only_unit_ids 只能引用输入中已有单元，不得把只读职责变成编辑任务。"
        "不要输出 updated_plan、chapter_updates、正文、替代措辞、外部答案或人工问题清单。"
        "每组 unit_ids 只能属于同一 chapter_id；不同章不能合组或自动拆组。\n完整选择输入："
        + json.dumps(visible, ensure_ascii=False, separators=(",", ":"))
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _known_units(payload: Mapping[str, Any]) -> tuple[dict[str, str], set[str]]:
    chapter_by_unit: dict[str, str] = {}
    readonly: set[str] = set(_ids(payload.get("selector_forbidden_unit_ids") or []))
    for chapter in payload.get("chapters") or []:
        chapter_id = _chapter_id(chapter)
        for row in _unit_rows(chapter):
            chapter_by_unit[_unit_id(row)] = chapter_id
    return chapter_by_unit, readonly


def _canonical_group_id(unit_ids: Sequence[str]) -> str:
    return "selection-" + _hash(sorted(str(value) for value in unit_ids))[:16]


def validate_selection_response(payload: Mapping[str, Any], parsed: Mapping[str, Any]) -> dict[str, Any]:
    """Validate selector structure without guessing invalid IDs or groups."""

    _verify_selection_payload(payload)
    errors: list[str] = []
    if not isinstance(parsed, Mapping):
        return {"status": "invalid", "groups": [], "validation_errors": ["selection_response_not_object"]}
    raw_status = str(parsed.get("status") or "").strip().casefold()
    if raw_status in _NO_SELECTION_STATUSES:
        if parsed.get("groups"):
            errors.append("no_change_with_groups")
        return {"status": "invalid" if errors else "no_change", "groups": [], "validation_errors": errors}
    if raw_status not in {"selected", "selection", "updated"}:
        return {"status": "invalid", "groups": [], "validation_errors": ["selection_status_required_or_invalid"]}
    raw_groups = parsed.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        return {"status": "invalid", "groups": [], "validation_errors": ["selected_groups_required"]}
    chapter_by_unit, known_readonly = _known_units(payload)
    known_units = set(chapter_by_unit)
    seen: set[str] = set()
    groups: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_groups):
        if not isinstance(raw, Mapping):
            errors.append(f"group_not_object:{index}")
            continue
        unit_ids = _ids(raw.get("unit_ids") or raw.get("modifiable_unit_ids"))
        if not unit_ids:
            errors.append(f"group_units_required:{index}")
            continue
        unknown = sorted(set(unit_ids) - known_units)
        if unknown:
            errors.append(f"unknown_unit_ids:{','.join(unknown)}")
        forbidden_selected = sorted(set(unit_ids) & known_readonly)
        if forbidden_selected:
            errors.append(f"readonly_unit_ids:{','.join(forbidden_selected)}")
        duplicate = sorted(set(unit_ids) & seen)
        if duplicate:
            errors.append(f"unit_repeated_across_groups:{','.join(duplicate)}")
        seen.update(unit_ids)
        chapter_ids = sorted({chapter_by_unit.get(value, "") for value in unit_ids if value in chapter_by_unit})
        if len(chapter_ids) > 1:
            errors.append(f"cross_chapter_group_forbidden:{index}:submit_same_chapter_local_clusters")
        explicit_chapters = _ids(raw.get("chapter_ids") or ([raw.get("chapter_id")] if raw.get("chapter_id") else []))
        if explicit_chapters and set(explicit_chapters) != set(chapter_ids):
            errors.append(f"group_chapter_ids_mismatch:{index}")
        related = _ids(raw.get("related_read_only_unit_ids") or raw.get("read_only_unit_ids") or [])
        related_unknown = sorted(set(related) - known_units)
        if related_unknown:
            errors.append(f"unknown_related_readonly_ids:{','.join(related_unknown)}")
        overlap = sorted(set(unit_ids) & set(related))
        if overlap:
            errors.append(f"editable_readonly_overlap:{','.join(overlap)}")
        if not str(raw.get("selection_reason") or "").strip():
            errors.append(f"selection_reason_required:{index}")
        focus = raw.get("improvement_focus")
        if not isinstance(focus, list) or not any(str(item).strip() for item in focus):
            errors.append(f"improvement_focus_required:{index}")
        groups.append({
            "group_id": _canonical_group_id(unit_ids),
            "model_group_id": str(raw.get("group_id") or "").strip(),
            "chapter_ids": chapter_ids,
            "unit_ids": unit_ids,
            "selection_reason": str(raw.get("selection_reason") or ""),
            "improvement_focus": _copy(focus or []),
            "related_read_only_unit_ids": related,
            "known_readonly_unit_ids": sorted(set(related) & known_readonly),
        })
    if errors:
        return {"status": "invalid", "groups": groups, "validation_errors": list(dict.fromkeys(errors))}
    return {"status": "selected", "groups": groups, "validation_errors": []}


def _role_from_unit(unit: Mapping[str, Any], chapter_id: str, *, reason: str) -> dict[str, Any]:
    role = _copy(unit)
    role["unit_id"] = _unit_id(unit)
    role["chapter_id"] = chapter_id
    role["read_only"] = True
    role["selection_boundary_reason"] = reason
    return role


def _current_unit_roles(
    chapter_payloads: Mapping[str, Mapping[str, Any]],
) -> dict[str, tuple[str, Mapping[str, Any]]]:
    """Index the latest complete unit duties by stable ID.

    Selection payloads are projected more than once as earlier owner results
    are merged into the current total outline.  The source payload for a later
    projection is authoritative; an older ``readonly_neighbor_unit_roles``
    entry is only a fallback when its unit no longer exists in that payload.
    """

    current: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for chapter_id, payload in chapter_payloads.items():
        for row in _unit_rows(payload):
            unit_id = _unit_id(row)
            if unit_id:
                current[unit_id] = (str(chapter_id), row)
    return current


def _chapter_sequence_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return compact complete-chapter order metadata for a local owner task.

    A projected payload contains only the selected editable units in
    ``chapter_plan.units``.  The owner still needs to know where those units
    sit in the complete chapter when it writes transitions.  Keep that
    information as stable IDs only; the full unit duties remain in the
    read-only role objects and are not duplicated here.
    """

    ordered = [_unit_id(row) for row in _unit_rows(payload) if _unit_id(row)]
    adjacent: dict[str, dict[str, str | None]] = {}
    for index, unit_id in enumerate(ordered):
        adjacent[unit_id] = {
            "previous_unit_id": ordered[index - 1] if index else None,
            "next_unit_id": ordered[index + 1] if index + 1 < len(ordered) else None,
        }
    return {
        "full_unit_order": ordered,
        "adjacent_unit_ids": adjacent,
        "order_source": "current_complete_chapter_payload",
    }


def project_selection_group(
    chapter_payloads: Mapping[str, Mapping[str, Any]], group: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Create chapter-local owner payloads while preserving group provenance."""

    unit_to_chapter: dict[str, str] = {}
    for chapter_id, payload in chapter_payloads.items():
        for row in _unit_rows(payload):
            unit_to_chapter[_unit_id(row)] = str(chapter_id)
    selected = _ids(group.get("unit_ids"))
    if not selected:
        raise ValueError("selection_projection_units_required")
    unknown = sorted(set(selected) - set(unit_to_chapter))
    if unknown:
        raise ValueError("selection_projection_unknown_units:" + ",".join(unknown))
    actual_chapters = sorted({unit_to_chapter[value] for value in selected})
    if len(actual_chapters) != 1:
        raise ValueError("cross_chapter_group_forbidden:submit_same_chapter_local_clusters;no_automatic_split")
    declared_chapters = _ids(group.get("chapter_ids") or ([group["chapter_id"]] if group.get("chapter_id") else []))
    if declared_chapters and set(declared_chapters) != set(actual_chapters):
        raise ValueError("selection_projection_chapter_ids_mismatch")
    source = chapter_payloads[actual_chapters[0]]
    allowed = set(_ids(source.get("modifiable_unit_ids") or []))
    forbidden = set(_ids(source.get("read_only_unit_ids") or []))
    if set(selected) - allowed or set(selected) & forbidden:
        raise ValueError("selection_projection_readonly_units")
    selected_set = set(selected)
    group_id = str(group.get("group_id") or _canonical_group_id(selected))
    related = set(_ids(group.get("related_read_only_unit_ids") or []))
    if related - set(unit_to_chapter):
        raise ValueError("selection_projection_unknown_related_readonly_units:" + ",".join(sorted(related - set(unit_to_chapter))))
    if related & selected_set:
        raise ValueError("selection_projection_editable_readonly_overlap")
    chapter_ids = actual_chapters
    current_roles = _current_unit_roles(chapter_payloads)
    def current_readonly_ids(unit_id: str) -> list[str]:
        descendants = [new_id for packet in chapter_payloads.values()
                       for new_id, ancestors in (packet.get("unit_id_remap") or {}).items()
                       if unit_id in ancestors and new_id in current_roles]
        resolved = ([unit_id] if unit_id in current_roles else []) + descendants
        # Truly external legacy context remains useful when no known lineage
        # replaces it. A known renamed role must not survive beside its update.
        return _ids(resolved) or [unit_id]

    outputs = []
    for chapter_id in chapter_ids:
        source = chapter_payloads.get(chapter_id)
        if not isinstance(source, Mapping):
            continue
        local_selected = [value for value in selected if unit_to_chapter.get(value) == chapter_id]
        if not local_selected:
            continue
        local_set = set(local_selected)
        projected = _copy(source)
        original_plan = projected.get("chapter_plan") if isinstance(projected.get("chapter_plan"), Mapping) else {}
        units = [row for row in original_plan.get("units") or [] if isinstance(row, Mapping) and _unit_id(row) in local_set]
        projected_plan = _copy(original_plan)
        projected_plan["units"] = units
        # Owner remaps describe this immediate revision, not historical lineage.
        projected_plan.pop("unit_id_remap", None)
        projected.pop("unit_id_remap", None)
        projected["chapter_plan"] = projected_plan
        context = _copy(projected.get("full_chapter_context") or {})
        context.update(_chapter_sequence_context(source))
        projected["full_chapter_context"] = context
        existing_roles = [row for row in projected.get("readonly_neighbor_unit_roles") or [] if isinstance(row, Mapping)]
        role_by_id = {}
        for row in existing_roles:
            old_id = _unit_id(row)
            if not old_id:
                continue
            for resolved_id in current_readonly_ids(old_id):
                if resolved_id in local_set:
                    continue
                current = current_roles.get(resolved_id)
                role_by_id[resolved_id] = (
                    _role_from_unit(current[1], current[0], reason="current_total_outline_context")
                    if current is not None else _copy(row)
                )
        # The current complete payload wins over a role copied from an older
        # projection.  Keep unknown legacy roles only as an audit fallback.
        for unit_id in list(role_by_id):
            current = current_roles.get(unit_id)
            if current is not None:
                owner_chapter, current_unit = current
                role_by_id[unit_id] = _role_from_unit(
                    current_unit, owner_chapter, reason="current_total_outline_context",
                )
        source_units = {_unit_id(row): row for row in _unit_rows(source)}
        # Every unselected unit in the current chapter is a read-only boundary.
        for unit_id, row in source_units.items():
            if unit_id not in local_set:
                role_by_id[unit_id] = _role_from_unit(row, chapter_id, reason="unselected_same_chapter_unit")
        # Related chapters are context only, never an editable group.
        for unit_id in selected_set | related:
            owner_chapter = unit_to_chapter.get(unit_id)
            if unit_id in local_set or not owner_chapter or owner_chapter == chapter_id:
                continue
            other = chapter_payloads.get(owner_chapter) or {}
            other_units = {_unit_id(row): row for row in _unit_rows(other)}
            if unit_id in other_units:
                role_by_id[unit_id] = _role_from_unit(other_units[unit_id], owner_chapter, reason="related_read_only_context")
        # Existing read-only IDs may not have had a role object in a prior
        # packet.  Resolve them from the latest total outline when possible.
        refreshed_readonly = _ids([new_id for old_id in _ids(projected.get("read_only_unit_ids") or [])
                                   for new_id in current_readonly_ids(old_id)])
        for unit_id in refreshed_readonly:
            if unit_id in local_set or unit_id in role_by_id:
                continue
            current = current_roles.get(unit_id)
            if current is not None:
                owner_chapter, current_unit = current
                role_by_id[unit_id] = _role_from_unit(
                    current_unit, owner_chapter, reason="current_total_outline_context",
                )
        readonly_ids = set(refreshed_readonly) | set(role_by_id)
        readonly_ids -= local_set
        projected["readonly_neighbor_unit_roles"] = list(role_by_id.values())
        projected["modifiable_unit_ids"] = local_selected
        projected["read_only_unit_ids"] = sorted(readonly_ids)
        # Keep the selector's machine-generated investment rationale visible to
        # the owner request as advisory provenance.  It is deliberately kept
        # outside chapter_plan/material channels and is never treated as fact.
        projected["selection_context"] = {
            "origin": "autonomous_unit_selector",
            "advisory_only": True,
            "group_id": group_id,
            "selection_reason": str(group.get("selection_reason") or ""),
            "improvement_focus": _copy(group.get("improvement_focus") or []),
            "group_chapter_ids": chapter_ids,
            "selected_unit_ids": selected,
            "related_read_only_unit_ids": sorted(related),
        }
        projected["call_id"] = f"outline-selection:{group_id}:{chapter_id}"
        contract = _copy(projected.get("unit_identity_contract") or {})
        contract["existing_unit_ids"] = list(local_selected)
        projected["unit_identity_contract"] = contract
        projected["input_integrity"] = strengthening._input_integrity(projected)
        strengthening._verify_envelope(projected)
        outputs.append({
            "group_id": group_id,
            "selection_reason": str(group.get("selection_reason") or ""),
            "improvement_focus": _copy(group.get("improvement_focus") or []),
            "group_chapter_ids": chapter_ids,
            "chapter_id": chapter_id,
            "modifiable_unit_ids": local_selected,
            "read_only_unit_ids": sorted(readonly_ids),
            "payload": projected,
        })
    return outputs


def selection_to_on_demand_payloads(
    chapter_payloads: Mapping[str, Mapping[str, Any]], selection: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if selection.get("validation_errors"):
        raise ValueError("selection_not_accepted_for_projection:validation_errors")
    if str(selection.get("status") or "").casefold() in {"none", "no_change"}:
        if selection.get("groups"):
            raise ValueError("selection_not_accepted_for_projection:no_change_with_groups")
        return []
    if str(selection.get("status") or "").casefold() != "selected":
        raise ValueError("selection_not_accepted_for_projection")
    outputs = []
    seen: set[str] = set()
    groups = selection.get("groups") or []
    if not isinstance(groups, list) or not groups:
        raise ValueError("selection_projection_groups_required")
    for group in groups:
        if not isinstance(group, Mapping):
            raise ValueError("selection_projection_group_not_object")
        unit_ids = _ids(group.get("unit_ids"))
        if seen & set(unit_ids):
            raise ValueError("selection_projection_unit_repeated_across_groups")
        seen.update(unit_ids)
        outputs.extend(project_selection_group(chapter_payloads, group))
    return outputs


def _checkpoint_signature(messages: Sequence[Mapping[str, Any]], *, model: str, thinking_budget: int, max_output_tokens: int, profile: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "selection_contract_version": SELECTION_SCHEMA,
        "request_sha256": _hash(messages),
        "model": str(model),
        "thinking_budget": int(thinking_budget),
        "max_output_tokens": int(max_output_tokens),
        "profile": _copy(profile or {}),
    }


def _checkpoint(path: str | Path | None, name: str) -> Path | None:
    if path is None:
        return None
    result = Path(path) / name
    result.parent.mkdir(parents=True, exist_ok=True)
    return result


def _write(path: Path | None, value: Mapping[str, Any]) -> None:
    if path is not None:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _load(path: Path | None, signature: Mapping[str, Any], resume: bool) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if value.get("signature") == dict(signature) and resume:
        return value
    # Never overwrite an old response when the prepared payload/profile has
    # changed.  Keep it available for audit while allowing a fresh call to be
    # prepared under the new signature.
    suffix = _hash(value)[:16]
    archived = path.with_name(f"{path.stem}.incompatible-{suffix}{path.suffix}")
    if not archived.exists():
        # If preservation fails, stop before replacing the old paid response.
        archived.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return None


def estimate_selection_request(messages: Sequence[Mapping[str, Any]], *, profile: Mapping[str, Any], token_counter: Any = None) -> dict[str, Any]:
    return strengthening.estimate_strengthening_request(messages, profile=profile, token_counter=token_counter)


def run_selection(
    payload: Mapping[str, Any], *, client: Any, model: str, thinking_budget: int, max_output_tokens: int,
    call_id: str | None = None, checkpoint_dir: str | Path | None = None, resume: bool = True,
    profile: Mapping[str, Any] | None = None, model_payload: Mapping[str, Any] | None = None,
    retry_unresolved: bool = False,
) -> dict[str, Any]:
    _verify_selection_payload(payload)
    if model_payload is None:
        model_payload = model_visible_selection_payload(payload)
    messages = selection_messages(payload, model_payload=model_payload)
    signature = _checkpoint_signature(messages, model=model, thinking_budget=thinking_budget, max_output_tokens=max_output_tokens, profile=profile)
    stage_path = _checkpoint(checkpoint_dir, "SELECTION_STAGE.json")
    stage = _load(stage_path, signature, resume)
    if (retry_unresolved and stage is not None
            and validate_selection_response(payload, stage.get("parsed_response") or {}).get("status") == "invalid"):
        from .outline_on_demand import _archive_rejected_checkpoints
        _archive_rejected_checkpoints((stage_path, _checkpoint(checkpoint_dir, "SELECTION_RAW_STAGE.json")), signature)
        stage = None
    raw_reused = False
    if stage is not None:
        raw = stage.get("raw_response") or {}
        parsed = stage.get("parsed_response") or {}
        telemetry = stage.get("telemetry") or {}
    else:
        raw_path = _checkpoint(checkpoint_dir, "SELECTION_RAW_STAGE.json")
        raw_stage = _load(raw_path, signature, resume)
        if raw_stage is not None:
            raw = raw_stage.get("raw_response") or {}
            raw_reused = True
        else:
            from .module4 import runtime
            raw = runtime.invoke_client(
                client, messages, model=model, max_output_tokens=int(max_output_tokens), thinking=True,
                thinking_budget=int(thinking_budget), call_id=call_id or str(payload.get("call_id") or "outline-selection"),
            )
            _write(raw_path, {"signature": signature, "raw_response": _copy(raw)})
        parsed, telemetry = planning._parse_planner_response(raw)
        validation = validate_selection_response(payload, parsed)
        result = {
            **validation, "parsed_response": parsed, "telemetry": telemetry, "raw_response": raw,
            "messages": messages, "request_sha256": _hash(messages), "raw_reused": raw_reused,
        }
        _write(stage_path, {"signature": signature, "result": result, "raw_response": _copy(raw), "parsed_response": _copy(parsed), "telemetry": _copy(telemetry)})
        return result
    validation = validate_selection_response(payload, parsed)
    return {
        **validation, "parsed_response": parsed, "telemetry": telemetry, "raw_response": raw,
        "messages": messages, "request_sha256": _hash(messages), "stage_reused": True,
    }


__all__ = [
    "SELECTION_SCHEMA", "SELECTION_ROLE", "build_selection_payload", "selection_messages",
    "validate_selection_response", "project_selection_group", "selection_to_on_demand_payloads",
    "model_visible_selection_payload", "verify_model_visible_projection",
    "estimate_selection_request", "run_selection",
]
