"""WO-01 bounded owner persistence checks; only the model boundary is fake."""
import copy
import json
from pathlib import Path

import pytest
from optomind_research.runtime.upgrade3.progressive_review_plan import (
    ProgressivePlannerConfig, ProgressiveReviewPlanner, _atomic_json,
    _messages_for, _chapter_review_source_materials, recover_compatible_chapter_details,
)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def packet(chapter_id="CH01", material=None):
    chapter = {"chapter_id": chapter_id, "title": chapter_id, "scope": "conditions", "source_ids": []}
    return {"topic_id": "topic", "research_question": "question", "chapter": chapter,
            "chapter_plan": {"thesis": "original", "reader_objective": "explain", "units": [
                {"unit_id": chapter_id + "_U01", "point": "conditions", "paragraph_briefs": [
                    {"point": "original", **({"source_handles": ["P0001"]} if material else {})}]}]},
            "source_materials": [material] if material else [], "candidate_materials": [],
            "candidate_navigation": {}, "shared_outline": {"chapters": [chapter]}}


def source(root, packets):
    for item in packets:
        cid = item["chapter"]["chapter_id"]
        revised = copy.deepcopy(item["chapter_plan"])
        revised["thesis"] = "recovered"
        revised["units"][0]["paragraph_briefs"].append({"point": "recovered detail"})
        _atomic_json(root / "stages/chapters" / (cid + ".json"), item)
        _atomic_json(root / "stages/affected_chapter_revision" / (cid + ".json"), {
            "chapter_id": cid, "status": "complete", "owner_status": "updated",
            "updated_plan": revised, "cache_inputs": {key: value for key, value in item.items() if key != "shared_outline"}})


class Boundary:
    def __init__(self, root, chapters):
        self.root, self.chapters, self.calls, self.fail = root, chapters, [], set()

    def __call__(self, stage, payload):
        self.calls.append((stage, copy.deepcopy(payload)))
        _atomic_json(self.root / "messages" / f"{len(self.calls):03d}.json", _messages_for(stage, payload))
        if stage == "whole_plan_improvement":
            return {"chapter_updates": [{"chapter_id": cid, "feedback": "clarify conditions"} for cid in self.chapters]}
        assert stage == "affected_chapter_revision"
        cid = payload["chapter_id"]
        if cid in self.fail:
            raise RuntimeError("offline owner seam failure")
        plan = copy.deepcopy(payload["chapter_plan"])
        plan["thesis"] = "owner-success-" + cid
        return {"status": "updated", "chapter_updates": [{"chapter_id": cid, "updated_plan": plan}]}


def review(root, packets, boundary, *, recovery_root=None, pool=(), resume=True, read_materials=None, planner_model="offline-owner-v1"):
    config = ProgressivePlannerConfig(topic_id="topic", pool_path=root / "unused-pool", plan_path=root / "unused-plan",
                                      output_dir=root, planning_revision_enabled=True, chapter_workers=1, planner_model=planner_model)
    planner = ProgressiveReviewPlanner(config, planner=boundary)
    if read_materials:
        planner._read_materials.update(read_materials)
    shared = packets[0]["shared_outline"]
    if recovery_root is not None:
        packets, report = recover_compatible_chapter_details(packets, recovery_root=recovery_root,
            shared_outline=shared, current_pool=pool, output_root=root)
        _atomic_json(root / "recovery_report.json", report)
    output, improvement, _ = planner._post_case_review(root=root, topic="question", harmonized={"shared_outline": shared},
        level1_outline={}, detail_records=packets, baseline_detail_records=packets, case_record={}, level1_tool_result={},
        level2_tool_result={}, chapter_tool_result={}, editorial_feedback={}, pool_rows=pool, resume=resume, state={})
    final = planner._assemble_final(topic="question", plan={}, pool_rows=pool, provisional={}, level1_outline={}, harmonized={},
        chapters=[row["chapter"] for row in packets], chapter_records=output, level1_tools={}, level2_tools={}, improvement=improvement)
    _atomic_json(root / "FINAL_FIXTURE.json", final)
    return read(root / "FINAL_FIXTURE.json")


def test_two_chapter_resume_keeps_first_owner_without_duplicate_call(tmp_path):
    packets = [packet("CH01"), packet("CH02")]
    shared = {"chapters": [row["chapter"] for row in packets]}
    for row in packets:
        row["shared_outline"] = shared
    source(tmp_path / "source", packets)
    boundary = Boundary(tmp_path / "run", ["CH01", "CH02"])
    boundary.fail = {"CH02"}
    first = review(tmp_path / "run", packets, boundary, recovery_root=tmp_path / "source")
    assert first["chapters"][0]["chapter_plan"]["thesis"] == "owner-success-CH01"
    boundary.fail = {"CH01"}
    final = review(tmp_path / "run", packets, boundary, recovery_root=tmp_path / "source")
    counts = {cid: sum(stage == "affected_chapter_revision" and value["chapter_id"] == cid for stage, value in boundary.calls)
              for cid in ("CH01", "CH02")}
    print("RESUME", counts, [row["chapter_plan"]["thesis"] for row in final["chapters"]])
    assert counts == {"CH01": 1, "CH02": 2}
    assert final["chapters"][0]["chapter_plan"]["thesis"] == "owner-success-CH01"
    assert len(final["chapters"][0]["chapter_plan"]["units"][0]["paragraph_briefs"]) == 2
    assert read(tmp_path / "run/stages/affected_chapter_revision/CH01.json")["status"] == "complete"


def test_failed_attempt_retains_separate_compatible_success(tmp_path):
    packets = [packet()]
    boundary = Boundary(tmp_path / "run", ["CH01"])
    review(tmp_path / "run", packets, boundary)
    boundary.fail = {"CH01"}
    failed = review(tmp_path / "run", packets, boundary, resume=False)
    assert read(tmp_path / "run/stages/affected_chapter_revision/CH01.json")["status"] == "failed"
    saved = read(tmp_path / "run/stages/affected_chapter_revision/successful/CH01.json")
    assert saved["updated_plan"]["thesis"] == "owner-success-CH01"
    assert failed["chapters"][0]["chapter_plan"]["thesis"] == "owner-success-CH01"
    final = review(tmp_path / "run", packets, boundary)
    assert len([stage for stage, _ in boundary.calls if stage == "affected_chapter_revision"]) == 2
    assert final["chapters"][0]["chapter_plan"]["thesis"] == "owner-success-CH01"


def test_recovery_normalizes_list_and_object_outlines(tmp_path):
    item = packet()
    source(tmp_path / "source", [item])
    item["shared_outline"] = item["shared_outline"]["chapters"]
    recovered, report = recover_compatible_chapter_details([item], recovery_root=tmp_path / "source",
        shared_outline=item["shared_outline"], output_root=tmp_path / "run")
    print("OUTLINE", report)
    assert report["recovered_chapters"] == 1
    assert read(tmp_path / "run/stages/recovered_chapters/CH01.json")["chapter_plan"]["thesis"] == "recovered"


def test_recovery_does_not_restore_corrected_supplement(tmp_path):
    material = {"source_handle": "P0001", "paper_id": "paper-1", "study_summary_A": {"finding": "same"},
                "review_planning_B": {"use": "same"}, "supplement_gap_material": {"text": "80 C"}}
    item = packet(material=material)
    source(tmp_path / "source", [item])
    item["source_materials"] = []
    card = tmp_path / "card.json"
    _atomic_json(card, {"general_understanding": material["study_summary_A"], "review_planning": material["review_planning_B"]})
    pool = [{"_source_handle": "P0001", "_paper_id": "paper-1", "card_path": str(card),
             "supplement_gap_material": {"text": "25 C"}}]
    result, report = recover_compatible_chapter_details([item], recovery_root=tmp_path / "source", current_pool=pool,
        shared_outline=item["shared_outline"], output_root=tmp_path / "run")
    _atomic_json(tmp_path / "run/RECOVERY_FIXTURE.json", {"records": result, "report": report})
    saved = read(tmp_path / "run/RECOVERY_FIXTURE.json")
    print("MATERIAL", saved)
    assert saved["report"]["recovered_chapters"] == 0
    assert "80 C" not in json.dumps(saved["records"])


def test_owner_messages_use_current_supplement_deep_and_local_material(tmp_path):
    stale = {"source_handle": "P0001", "paper_id": "paper-1", "study_summary_A": {"finding": "same"},
             "review_planning_B": {"use": "same"}, "supplement_gap_material": {"text": "old supplement"},
             "deep_read_material": {"content": {"finding": "old deep"}},
             "local_passages": {"passages": [{"text": "old local"}]}}
    item = packet(material=stale)
    card = tmp_path / "card.json"
    _atomic_json(card, {"general_understanding": stale["study_summary_A"], "review_planning": stale["review_planning_B"]})
    pool = [{"_source_handle": "P0001", "_paper_id": "paper-1", "card_path": str(card),
             "supplement_gap_material": {"text": "corrected supplement"},
             "local_passages": {"passages": [{"text": "corrected local"}]}}]
    boundary = Boundary(tmp_path / "run", ["CH01"])
    final = review(tmp_path / "run", [item], boundary, pool=pool,
                   read_materials={"paper-1": {"content": {"finding": "corrected deep"}}})
    owner_inputs = read(tmp_path / "run/stages/affected_chapter_revision/CH01.json")["cache_inputs"]
    messages = read(tmp_path / "run/messages/002.json")
    for current in ("corrected supplement", "corrected deep", "corrected local"):
        assert current in json.dumps(owner_inputs)
        assert current in json.dumps(messages)
        assert current in json.dumps(final["chapters"][0]["source_materials"])
    for previous in ("old supplement", "old deep", "old local"):
        assert previous not in json.dumps(owner_inputs["source_materials"])


def test_owner_reuses_material_when_only_paths_and_fetch_times_change(tmp_path):
    material = {"source_handle": "P0001", "paper_id": "paper-1", "study_summary_A": {"year": 2024},
                "supplement_gap_material": {"text": "result", "result_path": "first.json", "fetched_at": "yesterday",
                                            "conditions": {"time": "30 minutes", "year": 2023}}}
    item = packet(material=material)
    boundary = Boundary(tmp_path / "run", ["CH01"])
    review(tmp_path / "run", [item], boundary)
    updated = copy.deepcopy(item)
    updated["source_materials"][0]["supplement_gap_material"].update(result_path="moved.json", fetched_at="today")
    review(tmp_path / "run", [updated], boundary)
    assert len([stage for stage, _ in boundary.calls if stage == "affected_chapter_revision"]) == 1
    updated["source_materials"][0]["supplement_gap_material"]["conditions"]["time"] = "60 minutes"
    review(tmp_path / "run", [updated], boundary)
    assert len([stage for stage, _ in boundary.calls if stage == "affected_chapter_revision"]) == 2
    assert read(tmp_path / "run/stages/affected_chapter_revision/CH01.json")["cache_inputs"]["source_materials"][0]["supplement_gap_material"]["conditions"]["time"] == "60 minutes"


def test_no_owner_work_preserves_normal_plan(tmp_path):
    item = packet()
    boundary = Boundary(tmp_path / "run", [])
    final = review(tmp_path / "run", [item], boundary)
    assert final["chapters"][0]["chapter_plan"] == item["chapter_plan"]
    assert [stage for stage, _ in boundary.calls] == ["whole_plan_improvement"]
    assert not (tmp_path / "run/stages/affected_chapter_revision/CH01.json").exists()


@pytest.mark.parametrize("field", ["study_summary_A", "review_planning_B", "deep_read_material", "supplement_gap_material", "local_passages"])
def test_recovery_rejects_changes_in_every_consumed_material_channel(tmp_path, field):
    material = {"source_handle": "P0001", "paper_id": "paper-1", field: {"text": "previous condition"}}
    item = packet(material=material)
    source(tmp_path / "source", [item])
    item["source_materials"][0][field] = {"text": "corrected condition"}
    result, report = recover_compatible_chapter_details([item], recovery_root=tmp_path / "source",
        shared_outline=item["shared_outline"], output_root=tmp_path / "run")
    _atomic_json(tmp_path / "run/report.json", report)
    assert read(tmp_path / "run/report.json")["recovered_chapters"] == 0
    assert result[0]["chapter_plan"]["thesis"] == "original"


def test_recovery_preserves_outline_constraints_and_chapter_ownership(tmp_path):
    item = packet()
    item["shared_outline"]["scope"] = "only controlled experiments"
    source(tmp_path / "source", [item])
    item["shared_outline"]["scope"] = "only observational studies"
    result, report = recover_compatible_chapter_details([item], recovery_root=tmp_path / "source",
        shared_outline=item["shared_outline"])
    assert report["chapters"]["CH01"]["fields"] == ["shared_outline"]
    assert result[0]["chapter_plan"]["thesis"] == "original"
    item["shared_outline"]["scope"] = "only controlled experiments"
    revision_path = tmp_path / "source/stages/affected_chapter_revision/CH01.json"
    revision = read(revision_path)
    revision["chapter_id"] = "CH02"
    _atomic_json(revision_path, revision)
    result, report = recover_compatible_chapter_details([item], recovery_root=tmp_path / "source", shared_outline=item["shared_outline"])
    assert "chapter_ownership" in report["chapters"]["CH01"]["fields"]


def test_wrong_chapter_owner_response_is_persisted_unresolved(tmp_path):
    class WrongChapter(Boundary):
        def __call__(self, stage, payload):
            response = super().__call__(stage, payload)
            if stage == "affected_chapter_revision":
                response["chapter_updates"][0]["chapter_id"] = "CH99"
            return response
    item = packet()
    boundary = WrongChapter(tmp_path / "run", ["CH01"])
    final = review(tmp_path / "run", [item], boundary)
    saved = read(tmp_path / "run/stages/affected_chapter_revision/CH01.json")
    assert saved["owner_status"] == "unresolved"
    assert saved["structural_errors"] == ["owner_response_chapter_mismatch"]
    assert final["chapters"][0]["chapter_plan"] == item["chapter_plan"]


def test_incompatible_success_not_reused_after_material_change(tmp_path):
    material = {"source_handle": "P0001", "paper_id": "paper-1", "supplement_gap_material": {"text": "first result"}}
    item = packet(material=material)
    boundary = Boundary(tmp_path / "run", ["CH01"])
    review(tmp_path / "run", [item], boundary)
    item["source_materials"][0]["supplement_gap_material"]["text"] = "corrected result"
    boundary.fail = {"CH01"}
    final = review(tmp_path / "run", [item], boundary)
    assert final["chapters"][0]["chapter_plan"]["thesis"] == "original"
    assert read(tmp_path / "run/stages/affected_chapter_revision/CH01.json")["status"] == "failed"
    assert read(tmp_path / "run/stages/affected_chapter_revision/successful/CH01.json")["updated_plan"]["thesis"] == "owner-success-CH01"


def test_existing_packet_cannot_hide_corrected_current_pool_material(tmp_path):
    material = {"source_handle": "P0001", "paper_id": "paper-1", "study_summary_A": {"finding": "same"},
                "supplement_gap_material": {"text": "old condition"}}
    item = packet(material=material)
    source(tmp_path / "source", [item])
    card = tmp_path / "card.json"
    _atomic_json(card, {"general_understanding": material["study_summary_A"]})
    pool = [{"_source_handle": "P0001", "_paper_id": "paper-1", "card_path": str(card),
             "supplement_gap_material": {"text": "corrected condition"}}]
    result, report = recover_compatible_chapter_details([item], recovery_root=tmp_path / "source", current_pool=pool,
        shared_outline=item["shared_outline"], output_root=tmp_path / "run")
    _atomic_json(tmp_path / "run/report.json", {"records": result, "report": report})
    saved = read(tmp_path / "run/report.json")
    assert saved["report"]["recovered_chapters"] == 0
    assert saved["records"][0]["source_materials"][0]["supplement_gap_material"]["text"] == "corrected condition"


def test_compatible_restoration_uses_current_storage_metadata(tmp_path):
    material = {"source_handle": "P0001", "paper_id": "paper-1", "study_summary_A": {"finding": "same"},
                "supplement_gap_material": {"text": "same result", "result_path": "old.json"}}
    item = packet(material=material)
    source(tmp_path / "source", [item])
    item["source_materials"] = []
    card = tmp_path / "card.json"
    _atomic_json(card, {"general_understanding": material["study_summary_A"]})
    pool = [{"_source_handle": "P0001", "_paper_id": "paper-1", "card_path": str(card),
             "supplement_gap_material": {"text": "same result", "result_path": "new.json"}}]
    _, report = recover_compatible_chapter_details([item], recovery_root=tmp_path / "source", current_pool=pool,
        shared_outline=item["shared_outline"], output_root=tmp_path / "run")
    assert report["recovered_chapters"] == 1
    restored = read(tmp_path / "run/stages/recovered_chapters/CH01.json")["source_materials"][0]
    assert restored["supplement_gap_material"]["result_path"] == "new.json"
    assert restored["card_path"] == str(card)


def test_recovery_reads_preserved_success_after_failed_historical_attempt(tmp_path):
    item = packet()
    source(tmp_path / "source", [item])
    latest = tmp_path / "source/stages/affected_chapter_revision/CH01.json"
    _atomic_json(latest.parent / "successful/CH01.json", read(latest))
    _atomic_json(latest, {"chapter_id": "CH01", "status": "failed", "owner_status": "unresolved"})
    _, report = recover_compatible_chapter_details([item], recovery_root=tmp_path / "source",
        shared_outline=item["shared_outline"], output_root=tmp_path / "run")
    assert report["recovered_chapters"] == 1
    assert read(tmp_path / "run/stages/recovered_chapters/CH01.json")["chapter_plan"]["thesis"] == "recovered"


def test_implicit_legacy_owner_cache_is_preserved_but_requires_new_attempt(tmp_path):
    packets = [packet()]
    boundary = Boundary(tmp_path / "run", ["CH01"])
    review(tmp_path / "run", packets, boundary)
    latest = tmp_path / "run/stages/affected_chapter_revision/CH01.json"
    success = latest.parent / "successful/CH01.json"
    legacy = read(latest)
    legacy.pop("owner_cache_contract")
    _atomic_json(latest, legacy)
    _atomic_json(success, legacy)
    boundary.fail = {"CH01"}
    final = review(tmp_path / "run", packets, boundary)
    assert len([stage for stage, _ in boundary.calls if stage == "affected_chapter_revision"]) == 2
    assert final["chapters"][0]["chapter_plan"]["thesis"] == "original"
    assert read(latest)["status"] == "failed"
    assert read(success) == legacy
    assert legacy in [read(path) for path in (latest.parent / "history/CH01").glob("*.json")]


def test_owner_model_contract_change_does_not_apply_incompatible_success(tmp_path):
    packets = [packet()]
    boundary = Boundary(tmp_path / "run", ["CH01"])
    review(tmp_path / "run", packets, boundary)
    latest = tmp_path / "run/stages/affected_chapter_revision/CH01.json"
    old_success = read(latest)
    boundary.fail = {"CH01"}
    final = review(tmp_path / "run", packets, boundary, planner_model="offline-owner-v2")
    assert len([stage for stage, _ in boundary.calls if stage == "affected_chapter_revision"]) == 2
    assert final["chapters"][0]["chapter_plan"]["thesis"] == "original"
    assert read(latest)["status"] == "failed"
    assert read(latest.parent / "successful/CH01.json") == old_success
    assert read(latest)["owner_cache_contract"] != old_success["owner_cache_contract"]


def test_explicit_historical_seed_survives_new_owner_failure(tmp_path):
    packets = [packet()]
    source(tmp_path / "source", packets)
    boundary = Boundary(tmp_path / "run", ["CH01"])
    boundary.fail = {"CH01"}
    final = review(tmp_path / "run", packets, boundary, recovery_root=tmp_path / "source")
    assert final["chapters"][0]["chapter_plan"]["thesis"] == "recovered"
    assert len(final["chapters"][0]["chapter_plan"]["units"][0]["paragraph_briefs"]) == 2
    assert read(tmp_path / "run/stages/affected_chapter_revision/CH01.json")["status"] == "failed"
    assert read(tmp_path / "source/stages/affected_chapter_revision/CH01.json")["status"] == "complete"


def test_structural_owner_success_reuses_remap_and_survives_failed_retry(tmp_path):
    class SplitBoundary(Boundary):
        def __call__(self, stage, payload):
            response = super().__call__(stage, payload)
            if stage == "affected_chapter_revision":
                revised = response["chapter_updates"][0]["updated_plan"]
                old_id = revised["units"][0]["unit_id"]
                first, second = copy.deepcopy(revised["units"][0]), copy.deepcopy(revised["units"][0])
                first["unit_id"], second["unit_id"] = "CH01_U01A", "CH01_U01B"
                revised["units"] = [first, second]
                response["unit_id_remap"] = {"CH01_U01A": [old_id], "CH01_U01B": [old_id]}
            return response
    packets = [packet()]
    boundary = SplitBoundary(tmp_path / "run", ["CH01"])
    first = review(tmp_path / "run", packets, boundary)
    success_path = tmp_path / "run/stages/affected_chapter_revision/successful/CH01.json"
    saved = read(success_path)
    assert saved["unit_id_remap"] == {"CH01_U01A": ["CH01_U01"], "CH01_U01B": ["CH01_U01"]}
    second = review(tmp_path / "run", packets, boundary)
    assert second["chapters"][0]["chapter_plan"] == first["chapters"][0]["chapter_plan"]
    assert len([stage for stage, _ in boundary.calls if stage == "affected_chapter_revision"]) == 1
    boundary.fail = {"CH01"}
    final = review(tmp_path / "run", packets, boundary, resume=False)
    assert final["chapters"][0]["chapter_plan"] == first["chapters"][0]["chapter_plan"]
    assert read(success_path) == saved
    assert read(success_path.parent.parent / "CH01.json")["status"] == "failed"
    assert final["whole_plan_improvement"]["owner_revision_attempt_failures"] == ["CH01"]


def test_recovery_compares_real_compacted_owner_deep_read_to_current_raw_packet(tmp_path):
    deep = {"content": {"finding": "duplicate overview"}, "open_questions": ["duplicated task"],
            "question_material": [{"question_id": "Q1", "explanation": "usable current answer", "reference_ids": ["R1"]}],
            "references": [{"reference_id": "R1", "title": "used source"}, {"reference_id": "R2", "title": "unused source"}]}
    material = {"source_handle": "P0001", "paper_id": "paper-1", "deep_read_material": deep}
    item = packet(material=material)
    source(tmp_path / "source", [item])
    revision_path = tmp_path / "source/stages/affected_chapter_revision/CH01.json"
    saved = read(revision_path)
    saved["cache_inputs"]["source_materials"] = _chapter_review_source_materials(item, include_deep_read=True)
    _atomic_json(revision_path, saved)
    assert "content" not in read(revision_path)["cache_inputs"]["source_materials"][0]["deep_read_material"]
    _, report = recover_compatible_chapter_details([item], recovery_root=tmp_path / "source",
        shared_outline=item["shared_outline"], output_root=tmp_path / "run")
    assert report["recovered_chapters"] == 1
    restored = read(tmp_path / "run/stages/recovered_chapters/CH01.json")
    assert restored["source_materials"][0]["deep_read_material"] == deep
    item["source_materials"][0]["deep_read_material"]["question_material"][0]["explanation"] = "corrected answer"
    _, changed = recover_compatible_chapter_details([item], recovery_root=tmp_path / "source", shared_outline=item["shared_outline"])
    assert changed["recovered_chapters"] == 0


def test_owner_messages_refresh_both_existing_candidate_material_copies(tmp_path):
    stale = {"source_handle": "P0001", "paper_id": "paper-1", "study_summary_A": {"finding": "same"},
             "supplement_gap_material": {"text": "STALE_CANDIDATE_SUPPLEMENT"},
             "deep_read_material": {"content": {"finding": "STALE_CANDIDATE_DEEP"}},
             "local_passages": {"passages": [{"text": "STALE_CANDIDATE_LOCAL"}]}}
    item = packet(material=copy.deepcopy(stale))
    item["candidate_materials"] = [copy.deepcopy(stale)]
    item["candidate_navigation"] = {"candidate_materials": [copy.deepcopy(stale)], "candidates": []}
    card = tmp_path / "card.json"
    _atomic_json(card, {"general_understanding": stale["study_summary_A"]})
    pool = [{"_source_handle": "P0001", "_paper_id": "paper-1", "card_path": str(card),
             "supplement_gap_material": {"text": "CURRENT_CANDIDATE_SUPPLEMENT"},
             "local_passages": {"passages": [{"text": "CURRENT_CANDIDATE_LOCAL"}]}}]
    boundary = Boundary(tmp_path / "run", ["CH01"])
    final = review(tmp_path / "run", [item], boundary, pool=pool,
                   read_materials={"paper-1": {"content": {"finding": "CURRENT_CANDIDATE_DEEP"}}})
    messages = read(tmp_path / "run/messages/002.json")
    inputs = read(tmp_path / "run/stages/affected_chapter_revision/CH01.json")["cache_inputs"]
    for key in ("SUPPLEMENT", "DEEP", "LOCAL"):
        # The source delta's previous_material legitimately records old text;
        # consumed source/candidate views must contain only the correction.
        for rows in (inputs["source_materials"], inputs["candidate_materials"], inputs["candidate_navigation"]["candidate_materials"]):
            assert "STALE_CANDIDATE_" + key not in json.dumps(rows)
            assert "CURRENT_CANDIDATE_" + key in json.dumps(rows)
    parsed_payload = json.loads(messages[1]["content"].split("\n", 1)[1].split("\n\n【本轮交付】", 1)[0])
    for key in ("candidate_materials", "candidate_navigation"):
        assert "STALE_CANDIDATE_" not in json.dumps(parsed_payload[key])
        assert parsed_payload[key] == inputs[key]
    assert [row["source_handle"] for row in final["chapters"][0]["candidate_materials"]] == ["P0001"]


def test_recovery_does_not_refresh_unselected_chapter(tmp_path):
    item = packet(material={"source_handle": "P0001", "paper_id": "paper-1", "supplement_gap_material": {"text": "original"}})
    pool = [{"_source_handle": "P0001", "_paper_id": "paper-1", "supplement_gap_material": {"text": "changed"}}]
    output, report = recover_compatible_chapter_details([item], recovery_root=tmp_path / "unused-source", current_pool=pool,
        shared_outline=item["shared_outline"], chapter_ids=["CH02"])
    assert output == [item]
    assert report["recovered_chapters"] == 0
    assert report["chapters"] == {}
