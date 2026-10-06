from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

SOURCE = Path(r"F:\OptoMind-branch-backups\20261005-final-consolidation-204632\source")
BASE = Path(r"F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny")
OUT = Path(r"F:\OptoMind-Review-2\outputs\outline_selection_20261006")
PACKET_DIR = BASE / "planning" / "writer_packets"
PLAN_PATH = BASE / "planning" / "DETAILED_REVIEW_PLAN.json"
ID_MAP_PATH = BASE / "arrangement_repaired" / "ID_MAP.json"

sys.path.insert(0, str(SOURCE))
from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
from optomind_research.runtime.upgrade3 import outline_on_demand
from optomind_research.runtime.upgrade3 import outline_selection as selection
from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3.quality_capacity import load_quality_profile


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def sha(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def enrich_plan(packet: dict, view) -> tuple[dict, list[dict]]:
    plan = copy.deepcopy(packet["chapter_plan"])
    plan["chapter_id"] = view.chapter_id
    mapping = []
    units = []
    for index, (raw, resolved) in enumerate(zip(plan.get("units") or [], view.units), start=1):
        enriched = copy.deepcopy(raw)
        enriched["unit_id"] = resolved.unit_id
        briefs = []
        for brief, resolved_brief in zip(enriched.get("paragraph_briefs") or [], resolved.paragraph_briefs):
            row = copy.deepcopy(brief)
            row["paragraph_id"] = resolved_brief.paragraph_id
            row["unit_id"] = resolved.unit_id
            briefs.append(row)
        enriched["paragraph_briefs"] = briefs
        units.append(enriched)
        mapping.append({
            "derived_unit_id": resolved.unit_id,
            "original_unit_index": index,
            "original_unit_id": raw.get("unit_id") or raw.get("id"),
            "paragraph_ids": [brief.paragraph_id for brief in resolved.paragraph_briefs],
            "matching_basis": "production chapter_arrangement IdMap and packet ordinal",
        })
    plan["units"] = units
    return plan, mapping


def roles(view, title: str) -> list[dict]:
    return [{
        "unit_id": unit.unit_id,
        "chapter_id": view.chapter_id,
        "chapter_title": title,
        "substantive_point": unit.substantive_point,
        "ordered_development": unit.ordered_development,
        "synthesis": unit.synthesis,
        "transition": unit.transition,
        "read_only": True,
    } for unit in view.units]


def main() -> None:
    source_plan = load(PLAN_PATH)
    packets = {f"Ch{index}": load(PACKET_DIR / f"Ch{index}.json") for index in range(1, 8)}
    views = {key: arranging.build_chapter_view(PACKET_DIR / f"{key}.json", id_map_path=ID_MAP_PATH) for key in packets}
    enriched_plans = {}
    unit_maps = {}
    for key, packet in packets.items():
        enriched_plans[key], unit_maps[key] = enrich_plan(packet, views[key])

    chapter_payloads = {}
    for key, packet in packets.items():
        view = views[key]
        neighbor_roles = []
        for other_key, other_view in views.items():
            if other_key == key:
                continue
            neighbor_roles.extend(roles(other_view, str(packets[other_key].get("chapter", {}).get("title") or other_key)))
        chapter = copy.deepcopy(packet.get("chapter") or {})
        chapter["chapter_id"] = view.chapter_id
        plan = enriched_plans[key]
        body_path = BASE / "body_assembly_final" / "chapters" / f"{key}.md"
        full_context = {
            "chapter_id": view.chapter_id,
            "title": chapter.get("title") or view.title,
            "position": copy.deepcopy(packet.get("position") or {}),
            "purpose": plan.get("purpose"),
            "scope": plan.get("scope"),
            "reader_objective": plan.get("reader_objective"),
            "adjacent_chapters": copy.deepcopy(packet.get("adjacent_chapters") or {}),
            "open_questions": copy.deepcopy(packet.get("open_questions") or []),
            # Tool feedback and writer handoff are runtime/old-stage copies;
            # the full downstream payload retains them, but the selector sees
            # the complete outline and chapter duties without that telemetry.
            "selector_context_exclusions": ["relevant_tool_feedback", "writer_handoff"],
        }
        chapter_payloads[view.chapter_id] = strengthening.build_strengthening_payload(
            research_question=str(packet.get("research_question") or source_plan.get("research_question") or ""),
            chapter_id=view.chapter_id,
            chapter_plan=plan,
            source_materials=packet.get("source_materials") or [],
            readonly_neighbor_unit_roles=neighbor_roles,
            full_chapter_context=full_context,
            chapter=chapter,
            # The first layer is an outline selector; body text stays in the
            # downstream full payload and is not repeated in selector messages.
            actual_local_body="",
            shared_outline=packet.get("shared_outline") or source_plan.get("shared_outline") or [],
            shared_scope=packet.get("shared_scope") or source_plan.get("shared_scope") or {},
            review_argument=packet.get("review_argument") or source_plan.get("review_argument") or "",
            review_argument_status=str(packet.get("review_argument_status") or source_plan.get("review_argument_status") or ""),
            review_argument_source=str(packet.get("review_argument_source") or source_plan.get("review_argument_source") or ""),
            source_identity_map=packet.get("source_identity_map") or {},
            candidate_materials=packet.get("candidate_materials") or [],
            candidate_navigation=packet.get("candidate_navigation") or {},
            tool_materials=packet.get("tool_materials") or [],
            modifiable_unit_ids=[unit.unit_id for unit in view.units],
            read_only_unit_ids=[row["unit_id"] for row in neighbor_roles],
            topic_id=str(packet.get("topic_id") or source_plan.get("topic_id") or ""),
            call_id=f"outline-unit-selection-20261006:{view.chapter_id}",
        )
        chapter_payloads[view.chapter_id]["selection_source_provenance"] = {
            "packet": str(PACKET_DIR / f"{key}.json"),
            "body_path_for_downstream_owner": str(body_path),
            "unit_mapping": unit_maps[key],
        }

    ordered_payloads = [chapter_payloads[views[key].chapter_id] for key in packets]
    first = packets["Ch1"]
    selection_payload = selection.build_selection_payload(
        ordered_payloads,
        research_question=str(first.get("research_question") or source_plan.get("research_question") or ""),
        shared_outline=first.get("shared_outline") or source_plan.get("shared_outline") or [],
        shared_scope=first.get("shared_scope") or source_plan.get("shared_scope") or {},
        review_argument=first.get("review_argument") or source_plan.get("review_argument") or "",
        review_argument_status=str(first.get("review_argument_status") or source_plan.get("review_argument_status") or ""),
        review_argument_source=str(first.get("review_argument_source") or source_plan.get("review_argument_source") or ""),
    )
    # First-layer selection is about task worth, not paper retrieval.  Keep
    # the complete source/material payload for the downstream owner, while the
    # default selector view sends no whole-pool identity directory.
    model_payload = selection.model_visible_selection_payload(selection_payload, include_material_index=False)
    projection_check = selection.verify_model_visible_projection(selection_payload, model_payload)
    messages = selection.selection_messages(selection_payload, model_payload=model_payload)
    profile = load_quality_profile("outline_selection")
    tokenizer = Path(r"F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json")
    counter = planning.qwen_local_token_counter(tokenizer) if tokenizer.is_file() else None
    estimate = selection.estimate_selection_request(messages, profile=profile, token_counter=counter)

    # A deterministic structural fixture exercises two non-adjacent chapters.
    ids_by_chapter = {chapter_id: [row["unit_id"] for row in payload["chapter_plan"]["units"]] for chapter_id, payload in chapter_payloads.items()}
    fixture_ids = [ids_by_chapter[views["Ch1"].chapter_id][0], ids_by_chapter[views["Ch5"].chapter_id][-1]]
    fixture_response = {
        "status": "selected",
        "groups": [{
            "group_id": "fixture-cross-chapter-group",
            "unit_ids": fixture_ids,
            "selection_reason": "Fixture only: exercise shared group projection across non-adjacent chapters.",
            "improvement_focus": ["Fixture only: preserve cross-chapter boundary context."],
            "related_read_only_unit_ids": [],
        }],
    }
    fixture_selection = selection.validate_selection_response(selection_payload, fixture_response)
    fixture_requests = []
    for projected in selection.selection_to_on_demand_payloads(chapter_payloads, fixture_selection):
        owner_payload = projected["payload"]
        catalog = outline_on_demand.build_material_catalog(owner_payload)
        access = outline_on_demand.access_messages(owner_payload, catalog)
        owner = outline_on_demand.owner_messages(owner_payload, catalog, outline_on_demand.full_catalog_trace(catalog))
        fixture_requests.append({
            "group_id": projected["group_id"],
            "chapter_id": projected["chapter_id"],
            "selection_context": owner_payload["selection_context"],
            "modifiable_unit_ids": projected["modifiable_unit_ids"],
            "read_only_unit_ids": projected["read_only_unit_ids"],
            "access_messages": access,
            "owner_messages": owner,
            "access_messages_sha256": sha(access),
            "owner_messages_sha256": sha(owner),
        })

    report = {
        "status": "prepared_no_paid_calls",
        "source_head_at_prepare": "a0b645e096a148382196b5aadd3dcf36c43c69d6",
        "no_model_call": True,
        "no_root_scientific_opinion_in_input": True,
        "historical_candidates_not_in_input": True,
        "chapter_count": len(chapter_payloads),
        "unit_count": len(selection_payload["all_unit_ids"]),
        "unit_ids_by_chapter": ids_by_chapter,
        "selection_payload_sha256": sha(selection_payload),
        "selection_messages_sha256": sha(messages),
        "selection_message_chars": len(json.dumps(messages, ensure_ascii=False, separators=(",", ":"))),
        "selection_estimate": estimate,
        "model_visible_projection": projection_check,
        "include_selection_material_index": False,
        "model_payload_sha256": sha(model_payload),
        "profile": profile,
        "material_index_record_counts": {
            row["chapter_id"]: {channel: len(rows) for channel, rows in (row.get("material_index") or {}).items()}
            for row in selection_payload["chapters"]
        },
        "selector_does_not_include_full_material_cards": True,
        "downstream_full_payloads_path": str(OUT / "FULL_CHAPTER_PAYLOADS.json"),
        "fixture_group_unit_ids": fixture_ids,
        "fixture_second_layer_request_count": len(fixture_requests),
    }
    dump(OUT / "FULL_CHAPTER_PAYLOADS.json", chapter_payloads)
    dump(OUT / "INPUT_SELECTION_PAYLOAD.json", selection_payload)
    dump(OUT / "MODEL_VISIBLE_SELECTION_PAYLOAD.json", model_payload)
    dump(OUT / "SELECTION_MESSAGES.json", {"messages": messages, "sha256": sha(messages), "profile": profile, "model_visible_projection": projection_check})
    dump(OUT / "FIXTURE_SELECTION_RESPONSE.json", {"response": fixture_response, "validated": fixture_selection})
    dump(OUT / "FIXTURE_SECOND_LAYER_REQUESTS.json", fixture_requests)
    dump(OUT / "PREPARE_REPORT.json", report)
    dump(OUT / "UNIT_ID_MAP.json", {"chapter_maps": unit_maps, "ids_by_chapter": ids_by_chapter})
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
