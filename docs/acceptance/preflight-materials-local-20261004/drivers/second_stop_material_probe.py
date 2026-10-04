"""Small, provider-free second-stop probe over real BODY materials.

The probe consumes saved outputs and the current production material helpers.
It writes only under the acceptance root's ``real_checks`` directory; it does
not modify production sources or the historical input assets.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

if __name__ == "__main__" and (
    "--allow-local-run" not in sys.argv[1:]
    or os.environ.get("OPTOMIND_ALLOW_LOCAL_ACCEPTANCE_RUN") != "1"
):
    raise SystemExit(
        "refusing archive probe by default; set OPTOMIND_ALLOW_LOCAL_ACCEPTANCE_RUN=1 "
        "and pass --allow-local-run for an explicitly authorized local run"
    )

ACCEPT = Path(__file__).resolve().parents[1]
WORKTREE = ACCEPT / "worktree"
RESULTS = ACCEPT / "real_checks" / "results"
RESULTS.mkdir(parents=True, exist_ok=True)

import sys
sys.path.insert(0, str(WORKTREE))
from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3 import review_unit_writer as writing

BODY02 = Path(os.environ.get("OPTOMIND_BODY02_ROOT", "<SET_OPTOMIND_BODY02_ROOT>"))
BODY_FULL = Path(os.environ.get("OPTOMIND_BODY_FULL_ROOT", "<SET_OPTOMIND_BODY_FULL_ROOT>"))
POOL_PATH = Path(os.environ.get("OPTOMIND_POOL_PATH", "<SET_OPTOMIND_POOL_PATH>"))
DIRECTED = BODY02 / ("runs/live/reader/paper1_initial/directed/" "00d3d83d6571a7d9c15adbb84e0c371ce46d15a4/" "dr-task-0a3c29a5c78a27910afc4081/" "DIRECTED_READING.json")
FORMAL_PACKET = BODY_FULL / "live_chain/FORMAL_PACKET_AFTER_OWNER.json"
ARRANGEMENT = BODY_FULL / "live_chain/CHAPTER_ARRANGEMENT.json"
LEDGER = Path(os.environ.get("OPTOMIND_LOCAL_LEDGER", "<SET_OPTOMIND_LOCAL_LEDGER>"))
REAL_DIRECTED = ACCEPT / "records/real_directed"


def require_explicit_local_run() -> None:
    """Require an explicit opt-in even though this probe is provider-free."""
    if "--allow-local-run" not in sys.argv[1:] or os.environ.get("OPTOMIND_ALLOW_LOCAL_ACCEPTANCE_RUN") != "1":
        raise SystemExit(
            "refusing archive probe by default; set OPTOMIND_ALLOW_LOCAL_ACCEPTANCE_RUN=1 "
            "and pass --allow-local-run for an explicitly authorized local run"
        )

def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")

def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def ledger_state() -> dict[str, Any]:
    with sqlite3.connect(LEDGER) as connection:
        rows = connection.execute("select status, count(*), coalesce(sum(actual_cny),0), coalesce(sum(amount_cny),0) from reservations group by status order by status").fetchall()
    by_status = [{"status": str(row[0]), "count": int(row[1]), "actual_cny": float(row[2]), "amount_cny": float(row[3])} for row in rows]
    return {
        "by_status": by_status,
        "settled_actual_cny": sum(row["actual_cny"] for row in by_status if row["status"] == "settled"),
        "reserved_cny": sum(row["amount_cny"] for row in by_status if row["status"] == "reserved"),
        "uncertain": [row for row in by_status if row["status"] not in {"settled", "reserved"}],
    }

def p0004_pool_row() -> dict[str, Any]:
    paper_id = "00d3d83d6571a7d9c15adbb84e0c371ce46d15a4"
    for line in POOL_PATH.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("paper_id") == paper_id:
            return {**row, "_paper_id": paper_id, "_source_handle": "P0004"}
    raise RuntimeError("P0004_missing_from_real_pool")

def legacy_loader_and_move_probe() -> dict[str, Any]:
    pool = p0004_pool_row()
    source_snapshot = planning._snapshot_for_candidate(pool)
    if source_snapshot is None:
        raise RuntimeError("P0004_snapshot_not_found")
    original_source_sha = sha(source_snapshot / "READING_VIEW.md") + sha(source_snapshot / "REFERENCES.json")
    states_root = RESULTS / "source_states"
    if states_root.exists():
        shutil.rmtree(states_root)
    state_names = ("original", "moved_same_content", "body_changed", "references_changed")
    for name in state_names:
        state_root = states_root / name
        snapshot = state_root / "snapshot"
        shutil.copytree(source_snapshot, snapshot)
        card_path = state_root / "PAPER_READING_CARD.json"
        shutil.copy2(pool["card_path"], card_path)
        dump(state_root / "BATCH_STATE.json", {"items": {"p0004": {"paper_id": pool["_paper_id"], "snapshot_dir": str(snapshot)}}})
        for filename in ("DIRECTED_READING.json", "PROMPT.json", "INPUT.json"):
            shutil.copy2(DIRECTED.parent / filename, state_root / filename)
        if name == "body_changed":
            body_path = snapshot / "READING_VIEW.md"
            body = body_path.read_text(encoding="utf-8")
            marker = "## ABSTRACT\n"
            if marker not in body:
                raise RuntimeError("abstract_anchor_missing")
            body_path.write_text(body.replace(marker, marker + "SECOND_STOP_ISOLATED_BODY_EDIT\n", 1), encoding="utf-8", newline="\n")
        elif name == "references_changed":
            refs_path = snapshot / "REFERENCES.json"
            refs = load(refs_path)
            refs["references"][0]["text"] = str(refs["references"][0].get("text") or "") + " SECOND_STOP_ISOLATED_REFERENCE_EDIT"
            dump(refs_path, refs)
    prior = planning.load_prior_readings([states_root / "original/DIRECTED_READING.json"], [pool])
    if len(prior) != 1:
        raise RuntimeError("real_prior_not_loaded")
    state_checks: dict[str, Any] = {}
    for name in state_names:
        root = states_root / name
        candidate = {**pool, "card_path": str(root / "PAPER_READING_CARD.json")}
        current = planning._directed_source_state(candidate)
        state_checks[name] = {"snapshot": str(planning._snapshot_for_candidate(candidate)), "source_hash": current.get("source_hash"), "prior_source_compatible": planning._prior_read_source_compatible(candidate, prior[0]), "identity_conflict": bool(current.get("material_identity_conflict"))}
    move_root = RESULTS / "legacy_move"
    if move_root.exists():
        shutil.rmtree(move_root)
    for name in ("a", "b"):
        target = move_root / name
        target.mkdir(parents=True, exist_ok=True)
        for filename in ("DIRECTED_READING.json", "PROMPT.json", "INPUT.json"):
            shutil.copy2(states_root / "original" / filename, target / filename)
    loaded_a = planning.load_prior_readings([move_root / "a/DIRECTED_READING.json"], [pool])
    loaded_b = planning.load_prior_readings([move_root / "b/DIRECTED_READING.json"], [pool])
    compact_a, compact_b = planning._compact_reading_material(loaded_a[0]), planning._compact_reading_material(loaded_b[0])
    config = planning.ProgressivePlannerConfig(topic_id="real-second-stop-cache-probe", pool_path=RESULTS / "POOL.placeholder.jsonl", plan_path=RESULTS / "PLAN.placeholder.json", output_dir=RESULTS / "cache_probe")
    planner = planning.ProgressiveReviewPlanner(config, planner=lambda *_args, **_kwargs: {})
    contract_a = planner._cache_contract("case_groups", {"source_materials": [compact_a]})
    contract_b = planner._cache_contract("case_groups", {"source_materials": [compact_b]})
    stable_a, stable_b = dict(compact_a), dict(compact_b)
    stable_a.pop("reused_from", None); stable_b.pop("reused_from", None)
    stable_contract_a = planner._cache_contract("case_groups", {"source_materials": [stable_a]})
    stable_contract_b = planner._cache_contract("case_groups", {"source_materials": [stable_b]})
    return {
        "prior_path": str(states_root / "original/DIRECTED_READING.json"),
        "states": state_checks,
        "expected_state_result": {"original": True, "moved_same_content": True, "body_changed": False, "references_changed": False},
        "natural_loader_move": {"loaded_a": len(loaded_a) == 1, "loaded_b": len(loaded_b) == 1, "same_source_hash": loaded_a[0].get("source_hash") == loaded_b[0].get("source_hash"), "reused_from_distinct": loaded_a[0].get("reused_from") != loaded_b[0].get("reused_from"), "case_cache_contract_equal_with_reused_from": contract_a == contract_b, "case_cache_contract_equal_after_path_strip_control": stable_contract_a == stable_contract_b},
        "source_preserved": {"directed_reading_original_sha256": sha(DIRECTED), "source_snapshot_body_and_refs_before_hash": original_source_sha, "source_snapshot_unchanged": original_source_sha == sha(source_snapshot / "READING_VIEW.md") + sha(source_snapshot / "REFERENCES.json")},
        "interpretation": "The actual loader accepts the copied same-content result after directory move. The isolated body/reference edit changes practical source_hash and is rejected. The later cache contract still includes reused_from, so equivalent moved answers produce different case_groups contracts; stripping that path is only a diagnostic control.",
    }

def compact_material_presence(row: Mapping[str, Any]) -> dict[str, Any]:
    deep = row.get("deep_read_material") if isinstance(row.get("deep_read_material"), Mapping) else {}
    questions = deep.get("question_material") if isinstance(deep, Mapping) else []
    if not questions and isinstance(deep.get("content"), Mapping):
        questions = deep["content"].get("question_material") or []
    first = questions[0] if questions and isinstance(questions[0], Mapping) else {}
    examples = first.get("examples") if isinstance(first.get("examples"), list) else []
    example = examples[0] if examples and isinstance(examples[0], Mapping) else {}
    return {"source_handle": row.get("source_handle"), "paper_id": row.get("paper_id"), "title": row.get("title"), "doi": row.get("doi"), "material_depth": row.get("material_depth"), "material_available": planning._owner_material_has_content(row), "has_A": bool(row.get("study_summary_A")), "has_B": bool(row.get("review_planning_B")), "has_deep": bool(deep), "deep_question_count": len(questions or []), "deep_first_has_finding": bool(first.get("finding") or example.get("finding")), "deep_first_has_conditions": bool(first.get("conditions") or example.get("conditions")), "review_provenance": row.get("review_provenance")}

def case_to_writer_probe() -> dict[str, Any]:
    packet = load(FORMAL_PACKET)
    source_rows = packet.get("source_materials") or []
    p0602 = next(row for row in source_rows if row.get("source_handle") == "P0602")
    p0582 = next(row for row in source_rows if row.get("source_handle") == "P0582")
    p0602_deep = copy.deepcopy(p0602.get("deep_read_material"))
    p0602_identity_candidate = {
        key: value for key, value in p0602.items()
        if key not in {"deep_read_material", "study_summary_A", "review_planning_B", "supplement_gap_material", "supplement_gap_materials", "tool_supplement_materials"}
    }
    p0602_identity_candidate.update({"_source_handle": "P0602", "_paper_id": p0602.get("paper_id")})

    # Controlled first-adoption packet: preserve the real unit and all other
    # sources, but remove P0602's existing row/case material. Its identity is
    # supplied as a candidate and its real deep result only via read_materials.
    first_packet = copy.deepcopy(packet)
    first_packet["source_materials"] = [row for row in source_rows if row.get("source_handle") != "P0602"]
    first_packet["source_identity_map"] = {
        key: value for key, value in (packet.get("source_identity_map") or {}).items() if key != "P0602"
    }
    chapter = first_packet.get("chapter") if isinstance(first_packet.get("chapter"), Mapping) else {}
    chapter["source_materials"] = [row for row in chapter.get("source_materials") or [] if row.get("source_handle") != "P0602"]
    first_packet["chapter"] = chapter
    for unit in (first_packet.get("chapter_plan") or {}).get("units") or []:
        if not isinstance(unit, dict):
            continue
        for key in ("supporting_studies", "concrete_studies", "cases_and_sources", "cases_and_references"):
            if isinstance(unit.get(key), list):
                unit[key] = [row for row in unit[key] if not isinstance(row, Mapping) or row.get("source_handle") != "P0602"]
    selected = planning._case_selection_material_rows(
        ["P0602"], [first_packet], [p0602_identity_candidate], {p0602.get("paper_id"): p0602_deep}
    )
    selected_by_handle = {row.get("source_handle"): row for row in selected}
    additions = {"additions": [{"unit_key": "CH02:1", "studies": [{"source_handle": "P0602", "contribution": "Use the review-derived original study under the stated PDAC model and microbiome-depletion condition."}]}]}
    attached = planning._attach_case_groups(
        [first_packet], additions, planning_revision=True, body_case_additions=True,
        candidate_rows=[p0602_identity_candidate], read_materials={p0602.get("paper_id"): p0602_deep},
    )[0]
    attached_unit = next(unit for unit in (attached.get("chapter_plan") or {}).get("units") or [] if unit.get("unit_id") == "CH02:U3")
    if "P0602" not in [row.get("source_handle") for row in attached_unit.get("supporting_studies") or []]:
        raise AssertionError("P0602_first_adoption_not_attached")
    attached_p0602 = next(row for row in attached.get("source_materials") or [] if row.get("source_handle") == "P0602")
    deep_questions = (attached_p0602.get("deep_read_material") or {}).get("question_material") or []
    first_question = deep_questions[0] if deep_questions and isinstance(deep_questions[0], Mapping) else {}
    first_examples = first_question.get("examples") if isinstance(first_question.get("examples"), list) else []
    first_example = first_examples[0] if first_examples and isinstance(first_examples[0], Mapping) else {}
    expected_finding = str(first_example.get("finding") or first_question.get("finding") or "")
    expected_conditions = str(first_example.get("conditions") or first_question.get("conditions") or "")
    if not expected_finding or not expected_conditions:
        raise AssertionError("P0602_real_deep_missing_finding_or_conditions")
    attached_path = RESULTS / "ATTACHED_PACKET.json"; dump(attached_path, attached)
    chapter_view = arranging.build_chapter_view(attached_path, id_map_path=RESULTS / "ID_MAP.json")
    arrangement = copy.deepcopy(load(ARRANGEMENT))
    arrangement["source_catalog"] = arranging.build_source_catalog(chapter_view, arrangement)
    arrangement["chapter_tool_materials"] = arranging.compact_chapter_tool_materials(chapter_view)
    arrangement_path = RESULTS / "ARRANGEMENT_FROM_REAL_PACKET.json"; dump(arrangement_path, arrangement)
    view_path = RESULTS / "ARRANGEMENT_INPUT_FROM_REAL_PACKET.json"; arranging.write_view(chapter_view, view_path)
    writer_view = writing.build_unit_view(arrangement_path, "CH02:U3", view_path=view_path)
    payload = writing.unit_payload(writer_view, language="zh")
    messages = writing.unit_messages(writer_view, language="zh", planning_revision=True)
    dump(RESULTS / "UNIT_PAYLOAD.json", payload)
    dump(RESULTS / "UNIT_MESSAGES.json", messages)
    writer_sources = {row.get("source_handle"): row for row in payload.get("sources") or []}
    catalog = arrangement.get("source_catalog") or {}
    p0582_deep = copy.deepcopy(p0582.get("deep_read_material"))
    p0582_ab_only = {key: value for key, value in p0582.items() if key != "deep_read_material"}
    ab_packet = copy.deepcopy(packet); ab_packet["source_materials"] = [p0582_ab_only if row.get("source_handle") == "P0582" else row for row in source_rows]
    ab_row = planning._case_selection_material_rows(["P0582"], [ab_packet], [], {p0582.get("paper_id"): p0582_deep})[0]
    writer_text = json.dumps(messages, ensure_ascii=False)
    if expected_finding not in writer_text or expected_conditions not in writer_text:
        raise AssertionError("P0602_finding_or_conditions_not_in_writer_messages")
    return {
        "formal_packet_sources": {handle: compact_material_presence(next(row for row in source_rows if row.get("source_handle") == handle)) for handle in ("P0602", "P0582")},
        "controlled_first_adoption": {
            "candidate_identity": compact_material_presence(p0602_identity_candidate),
            "selection_rows": {handle: compact_material_presence(row) for handle, row in selected_by_handle.items()},
            "existing_P0602_source_removed_before_selection": not any(row.get("source_handle") == "P0602" for row in first_packet.get("source_materials") or []),
            "attached_supporting_studies": [row.get("source_handle") for row in attached_unit.get("supporting_studies") or []],
            "attached_source": compact_material_presence(attached_p0602),
            "exact_finding": expected_finding,
            "exact_conditions": expected_conditions,
            "exact_finding_in_writer_messages": expected_finding in writer_text,
            "exact_conditions_in_writer_messages": expected_conditions in writer_text,
        },
        "attached_case_units": [{"unit_id": unit.get("unit_id"), "supporting_studies": [study.get("source_handle") for study in unit.get("supporting_studies") or []]} for unit in (attached.get("chapter_plan") or {}).get("units") or []],
        "catalog": {handle: compact_material_presence(catalog.get(handle) or {}) for handle in ("P0602", "P0582")},
        "writer_payload_sources": {handle: compact_material_presence(writer_sources.get(handle) or {}) for handle in ("P0602", "P0582")},
        "writer_messages": {"message_count": len(messages), "P0602_occurrences": json.dumps(messages, ensure_ascii=False).count("P0602"), "P0582_occurrences": json.dumps(messages, ensure_ascii=False).count("P0582"), "P0602_conditions_present": bool((writer_sources.get("P0602") or {}).get("deep_read_material")), "source_catalog_path": str(arrangement_path)},
        "ab_row_shadow_temporal_probe": {"formal_P0582_has_A": bool(p0582.get("study_summary_A")), "formal_P0582_has_B": bool(p0582.get("review_planning_B")), "formal_P0582_has_deep": bool(p0582.get("deep_read_material")), "later_independent_deep_supplied": True, "selection_row_keeps_existing_AB": bool(ab_row.get("study_summary_A")) and bool(ab_row.get("review_planning_B")), "selection_row_merges_later_deep": bool(ab_row.get("deep_read_material")), "interpretation": "At the actual formal-packet time P0582 already contains A/B and deep, so the writer path is complete. A temporal fixture that removes only deep from that existing A/B row shows that the current helper does not merge a separately supplied later deep when the existing row is already substantive; this is a bounded ordering seam, not evidence that the real packet lost P0582 deep."},
    }

def main() -> None:
    before = ledger_state()
    result = {"schema_version": "body_preflight_material_acceptance.real_checks.v1", "commit": "6d1c8525fe0c2bcad53fcaec7699c88e04e63503", "provider_calls": 0, "paid_model_calls": 0, "ledger_before": before, "legacy_loader_move": legacy_loader_and_move_probe(), "case_to_writer": case_to_writer_probe()}
    after = ledger_state(); result["ledger_after"] = after; result["ledger_unchanged"] = before == after
    dump(RESULTS / "SECOND_STOP_RESULT.json", result)
    print(json.dumps({"result": str(RESULTS / "SECOND_STOP_RESULT.json"), "provider_calls": 0, "ledger_unchanged": result["ledger_unchanged"], "original_compatible": result["legacy_loader_move"]["states"]["original"]["prior_source_compatible"], "moved_compatible": result["legacy_loader_move"]["states"]["moved_same_content"]["prior_source_compatible"], "body_changed_compatible": result["legacy_loader_move"]["states"]["body_changed"]["prior_source_compatible"], "references_changed_compatible": result["legacy_loader_move"]["states"]["references_changed"]["prior_source_compatible"], "P0602_writer_deep": result["case_to_writer"]["writer_payload_sources"]["P0602"]["has_deep"], "P0602_writer_conditions": result["case_to_writer"]["writer_payload_sources"]["P0602"]["deep_first_has_conditions"]}, ensure_ascii=False))

if __name__ == "__main__":
    require_explicit_local_run()
    main()
