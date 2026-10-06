from __future__ import annotations
import copy
import hashlib
import json
import sys
from pathlib import Path

SOURCE = Path(r"F:\OptoMind-branch-backups\20261005-final-consolidation-204632\source")
BASE = Path(r"F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny")
OUT = Path(r"F:\OptoMind-Review-2\outputs\outline_full_strengthening_20261006_40cny\ch5_full_material")
PLAN_PATH = BASE / "planning" / "DETAILED_REVIEW_PLAN.json"
PACKET_DIR = BASE / "planning" / "writer_packets"
ID_MAP_PATH = BASE / "arrangement_repaired" / "ID_MAP.json"
BODY_PATH = BASE / "body_assembly_final" / "chapters" / "Ch5.md"

sys.path.insert(0, str(SOURCE))
from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
from optomind_research.runtime.upgrade3 import outline_on_demand as on_demand
from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3.quality_capacity import load_quality_profile


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def sha(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()


def serialised_chars(messages):
    return len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))


def unit_role(view, chapter_title):
    return [
        {
            "unit_id": unit.unit_id,
            "chapter_id": view.chapter_id,
            "chapter_title": chapter_title,
            "substantive_point": unit.substantive_point,
            "ordered_development": unit.ordered_development,
            "synthesis": unit.synthesis,
            "transition": unit.transition,
            "read_only": True,
        }
        for unit in view.units
    ]


def enrich_plan(packet, view):
    plan = copy.deepcopy(packet["chapter_plan"])
    plan["chapter_id"] = view.chapter_id
    units = []
    mapping = []
    for index, (raw, resolved) in enumerate(zip(plan.get("units") or [], view.units), start=1):
        enriched = copy.deepcopy(raw)
        enriched["unit_id"] = resolved.unit_id
        original_briefs = list(enriched.get("paragraph_briefs") or [])
        briefs = []
        for ordinal, (brief, resolved_brief) in enumerate(zip(original_briefs, resolved.paragraph_briefs), start=1):
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
            "matching_basis": "production IdMap unit signature: chapter_id + original ordinal + substantive_point",
            "paragraph_ids": [brief.paragraph_id for brief in resolved.paragraph_briefs],
            "substantive_point_sha256": hashlib.sha256(resolved.substantive_point.encode("utf-8")).hexdigest(),
        })
    plan["units"] = units
    return plan, mapping


source_plan = load(PLAN_PATH)
packet = load(PACKET_DIR / "Ch5.json")
ch4_packet = load(PACKET_DIR / "Ch4.json")
ch6_packet = load(PACKET_DIR / "Ch6.json")
view = arranging.build_chapter_view(PACKET_DIR / "Ch5.json", id_map_path=ID_MAP_PATH)
ch4_view = arranging.build_chapter_view(PACKET_DIR / "Ch4.json", id_map_path=ID_MAP_PATH)
ch6_view = arranging.build_chapter_view(PACKET_DIR / "Ch6.json", id_map_path=ID_MAP_PATH)
chapter_plan, unit_mapping = enrich_plan(packet, view)
readonly_roles = unit_role(ch4_view, str(ch4_packet.get("chapter", {}).get("title") or "Ch4")) + unit_role(
    ch6_view, str(ch6_packet.get("chapter", {}).get("title") or "Ch6")
)
chapter = copy.deepcopy(packet.get("chapter") or {})
chapter["chapter_id"] = view.chapter_id
full_context = {
    "chapter_id": view.chapter_id,
    "title": chapter.get("title") or view.title,
    "position": copy.deepcopy(packet.get("position") or {}),
    "purpose": chapter_plan.get("purpose"),
    "scope": chapter_plan.get("scope"),
    "reader_objective": chapter_plan.get("reader_objective"),
    "adjacent_chapters": copy.deepcopy(packet.get("adjacent_chapters") or {}),
    "open_questions": copy.deepcopy(packet.get("open_questions") or []),
    "relevant_tool_feedback": copy.deepcopy(packet.get("relevant_tool_feedback") or []),
    "writer_handoff": copy.deepcopy(packet.get("writer_handoff") or {}),
}

payload = strengthening.build_strengthening_payload(
    research_question=str(packet.get("research_question") or source_plan.get("research_question") or ""),
    chapter_id=view.chapter_id,
    chapter_plan=chapter_plan,
    source_materials=packet.get("source_materials") or [],
    readonly_neighbor_unit_roles=readonly_roles,
    full_chapter_context=full_context,
    chapter=chapter,
    actual_local_body=BODY_PATH.read_text(encoding="utf-8"),
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
    read_only_unit_ids=[row["unit_id"] for row in readonly_roles],
    topic_id=str(packet.get("topic_id") or source_plan.get("topic_id") or ""),
    call_id="outline-full-strengthening-20261006:Ch5",
)

full_profile = load_quality_profile("strong_outline_chapter")
access_profile = load_quality_profile("autonomous_outline")
full_messages = strengthening.strengthening_messages(payload)
catalog = on_demand.build_material_catalog(payload)
access_messages = on_demand.access_messages(payload, catalog)
upper_trace = on_demand.full_catalog_trace(catalog)
demand_owner_messages = on_demand.owner_messages(payload, catalog, upper_trace)

tokenizer = Path(r"F:\\OptoMind-Review-2\\data\\tokenizers\\qwen3_5_9b\\tokenizer.json")
counter = planning.qwen_local_token_counter(tokenizer) if Path(tokenizer).is_file() else None
full_estimate = strengthening.estimate_strengthening_request(full_messages, profile=full_profile, token_counter=counter)
access_estimate = strengthening.estimate_strengthening_request(access_messages, profile=access_profile, token_counter=counter)
demand_owner_estimate = strengthening.estimate_strengthening_request(demand_owner_messages, profile=full_profile, token_counter=counter)
full_chars = serialised_chars(full_messages)
demand_chars = serialised_chars(demand_owner_messages)
ratio = demand_chars / full_chars if full_chars else None
# This is a preparation-time mode suggestion only. The paid boundary remains external.
context_limit_assumption = 1000000
full_fits_assumed_context = int(full_estimate["total_context_tokens"]) <= context_limit_assumption
recommendation = "full_material_max" if full_fits_assumed_context and ratio is not None and ratio >= 0.90 else "on_demand_max"

provenance = {
    "source_head_at_prepare": "a0b645e096a148382196b5aadd3dcf36c43c69d6",
    "input_plan": str(PLAN_PATH),
    "input_packet": str(PACKET_DIR / "Ch5.json"),
    "adjacent_packets": [str(PACKET_DIR / "Ch4.json"), str(PACKET_DIR / "Ch6.json")],
    "id_map": str(ID_MAP_PATH),
    "body": str(BODY_PATH),
    "no_model_call": True,
    "no_root_scientific_opinion_in_input": True,
    "historical_candidates_not_in_input": True,
}
dump(OUT / "INPUT_PAYLOAD.json", payload)
dump(OUT / "UNIT_ID_MAP.json", {
    "chapter_id": view.chapter_id,
    "production_id_map_path": str(ID_MAP_PATH),
    "mapping": unit_mapping,
    "neighbor_unit_ids_readonly": [row["unit_id"] for row in readonly_roles],
})
dump(OUT / "FULL_MAX_MESSAGES.json", {"messages": full_messages, "sha256": sha(full_messages), "profile": full_profile})
dump(OUT / "DEMAND_ACCESS_MESSAGES.json", {"messages": access_messages, "sha256": sha(access_messages), "profile": access_profile})
dump(OUT / "DEMAND_OWNER_UPPER_BOUND_MESSAGES.json", {"messages": demand_owner_messages, "sha256": sha(demand_owner_messages), "profile": full_profile})
dump(OUT / "MATERIAL_CATALOG.json", on_demand.public_catalog(catalog))
dump(OUT / "PROVENANCE.json", provenance)
dump(OUT / "PREPARE_REPORT.json", {
    "status": "prepared_no_paid_calls",
    "chapter_id": view.chapter_id,
    "chapter_title": view.title,
    "units": len(view.units),
    "modifiable_unit_ids": [unit.unit_id for unit in view.units],
    "read_only_unit_count": len(readonly_roles),
    "read_only_unit_ids": [row["unit_id"] for row in readonly_roles],
    "source_material_count": len(payload["source_materials"]),
    "candidate_material_count": len(payload["candidate_materials"]),
    "tool_material_count": len(payload["tool_materials"]),
    "source_identity_count": len(payload["source_identity_map"]),
    "actual_body_chars": len(payload["actual_local_body"]),
    "input_payload_sha256": sha(payload),
    "full_messages_sha256": sha(full_messages),
    "demand_access_messages_sha256": sha(access_messages),
    "demand_owner_messages_sha256": sha(demand_owner_messages),
    "serialized_message_chars": {
        "full_material_max": full_chars,
        "demand_access_plus": serialised_chars(access_messages),
        "demand_owner_upper_bound_max": demand_chars,
    },
    "tokenizer": str(tokenizer),
    "profiles": {
        "full_material_max": full_profile,
        "demand_access_plus": access_profile,
        "demand_owner_max": full_profile,
    },
    "estimates": {
        "full_material_max": full_estimate,
        "demand_access_plus": access_estimate,
        "demand_owner_upper_bound_max": demand_owner_estimate,
        "demand_access_plus_plus_owner_upper_bound_max": {
            "estimated_cost_cny": float(access_estimate["estimated_cost_cny"]) + float(demand_owner_estimate["estimated_cost_cny"]),
            "input_reserved_tokens": int(access_estimate["reserved_input_tokens"]) + int(demand_owner_estimate["reserved_input_tokens"]),
        },
    },
    "demand_owner_to_full_serialized_char_ratio": ratio,
    "context_limit_assumption_tokens": context_limit_assumption,
    "full_material_fits_context_assumption": full_fits_assumed_context,
    "preparation_mode_recommendation": recommendation,
    "recommendation_basis": "If the complete material owner request fits the 1000000-token production pricing/context assumption and the demand upper-bound owner request is within 10% of its serialized size, direct Max avoids the Plus selector; this is a preparation heuristic, not a paid decision.",
    "catalog_coverage": catalog.get("coverage") or {},
    "catalog_entry_count": len(catalog.get("entries") or []),
    "upper_bound_unique_selected_count": int(upper_trace.get("resolved_count") or 0),
    "upper_bound_duplicate_access_ids_omitted": upper_trace.get("duplicate_access_ids_omitted") or [],
    "provenance": provenance,
})
print(json.dumps({
    "status": "prepared_no_paid_calls",
    "chapter": view.chapter_id,
    "units": len(view.units),
    "source_materials": len(payload["source_materials"]),
    "tool_materials": len(payload["tool_materials"]),
    "full_prompt_tokens": full_estimate["prompt_tokens_estimate"],
    "demand_prompt_tokens": demand_owner_estimate["prompt_tokens_estimate"],
    "full_cost_estimate": full_estimate["estimated_cost_cny"],
    "demand_cost_estimate": float(access_estimate["estimated_cost_cny"]) + float(demand_owner_estimate["estimated_cost_cny"]),
    "recommendation": recommendation,
}, ensure_ascii=False))


