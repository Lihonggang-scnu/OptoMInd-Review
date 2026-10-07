"""Offline OWNER adoption and cumulative complete-record handoff contracts."""
from copy import deepcopy
import json

import pytest

from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
from optomind_research.runtime.upgrade3 import outline_on_demand as demand
from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3 import review_unit_writer as writing
from test_outline_strengthening import _payload, _plan, _source
from test_outline_on_demand_navigation import _payload as navigation_payload


ARGS = dict(access_model="qwen3.5-plus", access_thinking_budget=8, access_max_output_tokens=8,
            owner_model="qwen3.8-max", owner_thinking_budget=8, owner_max_output_tokens=8)


def raw(value):
    return {"content": json.dumps(value), "complete": True, "finish_reason": "stop"}


def owner_run(payload, requests, handles):
    seen = []
    changed = _plan("Use the supplied findings under their recorded conditions")
    changed["units"][0]["paragraph_briefs"][0]["source_handles"] = handles

    def owner(messages, **_kwargs):
        seen.extend(deepcopy(messages))
        return raw({"status": "updated", "updated_plan": changed})

    result = demand.run_on_demand_strengthening(
        payload, access_client=lambda *_args, **_kwargs: raw({"status": "partial", "material_requests": requests}),
        owner_client=owner, **ARGS)
    return result, seen


def downstream_view(tmp_path, payload, result):
    packet = strengthening.project_plan_for_arrangement(payload, result)
    path = tmp_path / "PACKET.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    return arranging.build_chapter_view(path, id_map_path=tmp_path / "ID_MAP.json")


@pytest.mark.parametrize("domain", ["waveguide loss", "classroom learning"])
@pytest.mark.parametrize("channel", ["source_materials", "candidate_materials", "candidate_navigation"])
@pytest.mark.parametrize("select_parent", [False, True])
def test_nested_review_original_survives_owner_adoption(tmp_path, domain, channel, select_parent):
    original = {
        "source_handle": "P0101", "paper_id": "original-study", "doi": "10.fixture/original",
        "title": "Review-reported original study",
        "reviewed_original_identity": {"paper_id": "original-study", "doi": "10.fixture/original"},
        "reporting_review": {"paper_id": "reporting-review"},
        "deep_read_material": {"question_material": [{
            "explanation": f"Observed {domain} finding",
            "examples": [{"finding": f"Original {domain} result", "conditions": f"Bounded {domain} setting",
                          "attribution": "Original study reported by review"}],
        }]},
    }
    parent = {**_source("P0100"), "sources": [original]}
    if channel == "candidate_navigation":
        payload = _payload(candidate_navigation={"candidate_materials": [parent]})
        parent_id = "candidate_navigation.candidate_materials[0]"
    elif channel == "source_materials":
        payload = _payload(source_materials=[_source(), parent])
        parent_id = "source_materials[1]"
    else:
        payload = _payload(candidate_materials=[parent])
        parent_id = "candidate_materials[0]"
    child_id = parent_id + ".sources[0]"
    requests = [{"access_id": child_id}]
    if select_parent:
        requests.insert(0, {"access_id": parent_id})
    before = deepcopy(payload)
    result, seen = owner_run(payload, requests, ["P0001", "P0101"])
    assert result["status"] == "updated", result["structural_errors"]
    assert f"Original {domain} result" in json.dumps(seen)
    # Existing cited sources do not need a redundant fresh full-record read.
    assert "Observed finding P0001" not in json.dumps(seen)
    accepted = {row["source_handle"]: row for row in result["accepted_source_materials"]}
    assert accepted["P0101"] == original
    assert accepted["P0001"] == before["source_materials"][0]
    assert "study_summary_A" not in accepted["P0101"]
    assert not result["adoption_material_resolution"]["unresolved"]
    assert payload == before
    view = downstream_view(tmp_path, payload, result)
    original_view = next(row for row in view.sources if row.source_handle == "P0101")
    assert original_view.deep_read_material == original["deep_read_material"]
    assert original_view.paper_id == original["paper_id"]
    assert original_view.doi == original["doi"]


@pytest.mark.parametrize("domain", ["optical pulse", "student feedback"])
def test_complementary_material_reaches_actual_owner_and_downstream_messages(tmp_path, domain):
    base = {**_source(), "doi": "10.fixture/same"}
    complement = deepcopy(base)
    singulars = ("study_summary_A", "review_planning_B", "deep_read_material", "supplement_material", "local_passages")
    plurals = ("study_summary_A_variants", "review_planning_B_variants", "deep_read_materials", "supplement_materials", "local_passages_variants")
    for key in singulars:
        text_key = "planning_summary" if key == "review_planning_B" else "finding"
        condition_key = "scope_interpretation_cautions" if key == "review_planning_B" else "conditions"
        base[key] = {text_key: f"BASE-{key}-{domain}", condition_key: f"BASE-CONDITION-{key}-{domain}"}
        complement[key] = {text_key: f"EXTRA-{key}-{domain}", condition_key: f"EXTRA-CONDITION-{key}-{domain}"}
    payload = _payload(source_materials=[base], candidate_materials=[complement])
    result, seen = owner_run(payload, [{"source_handle": "P0001"}], ["P0001"])
    assert result["status"] == "updated", result["structural_errors"]
    assert all(f"EXTRA-{key}-{domain}" in json.dumps(seen) for key in singulars)
    accepted = result["accepted_source_materials"][0]
    for singular, plural in zip(singulars, plurals):
        assert accepted[singular] == base[singular]
        assert accepted[plural] == [complement[singular]]
        # Adopted variants remain discoverable in a subsequent on-demand run.
        assert f"EXTRA-{singular}-{domain}" in json.dumps(demand._locator_summary(accepted))
    # Closing again, including duplicate offers, must neither duplicate nor erase variants.
    closed, report = planning._resolve_owner_source_materials(
        source_materials=[accepted], chapter_plan=result["updated_plan"],
        candidate_materials=[complement, deepcopy(complement)])
    assert closed == [accepted]
    assert not report["unresolved"]
    view = downstream_view(tmp_path, payload, result)
    source = view.sources[0].to_dict()
    for singular, plural in zip(singulars, plurals):
        assert source[plural] == [complement[singular]]
    brief = view.units[0].paragraph_briefs[0]
    arrangement_response = {"chapter_id": view.chapter_id, "units": [{
        "unit_id": view.units[0].unit_id, "paragraph_tasks": [{
            "paragraph_id": brief.paragraph_id, "source_briefs": [brief.paragraph_id],
            "point": brief.point, "development": brief.development,
            "source_uses": [{"source_handle": "P0001"}],
        }],
    }]}
    editor_messages = []

    def editor(messages, **_kwargs):
        editor_messages.extend(deepcopy(messages))
        return raw(arrangement_response)

    arrangement = arranging.run_arrangement(
        view, client=editor, model="offline", view_payload=view.arrangement_payload(), prompt="Offline material handoff")
    for key in ("study_summary_A", "review_planning_B"):
        assert f"EXTRA-{key}-{domain}" in json.dumps(editor_messages)
        assert f"EXTRA-CONDITION-{key}-{domain}" in json.dumps(editor_messages)
    assert arrangement["validation"]["ok"]
    path = tmp_path / "CHAPTER_ARRANGEMENT.json"
    path.write_text(json.dumps({**arrangement, "source_catalog": arranging.build_source_catalog(view, arrangement)}))
    arranging.write_view(view, tmp_path / "ARRANGEMENT_INPUT.json")
    writer = writing.build_unit_view(path, view.units[0].unit_id)
    writer_messages = []

    def write(messages, **_kwargs):
        writer_messages.append(deepcopy(messages))
        return raw({"body_markdown": "Offline supported finding [P0001].", "status": "appended",
                    "covered_task_ids": [brief.paragraph_id]})

    writing.run_unit_writing(writer, client=write, model="offline", prompt="Offline material handoff")
    writing.run_unit_completion(writer, existing_body="Existing body", task_ids=[brief.paragraph_id],
                                client=write, model="offline", prompt="Offline material handoff")
    assert len(writer_messages) == 2
    for messages in writer_messages:
        for key in singulars:
            for marker in ("BASE-", "EXTRA-", "BASE-CONDITION-", "EXTRA-CONDITION-"):
                assert f"{marker}{key}-{domain}" in json.dumps(messages)


@pytest.mark.parametrize("identity", [{"paper_id": "foreign-study"}, {"doi": "10.fixture/foreign"},
                                      {"canonical_paper_id": "foreign-study"}])
def test_complementary_conflicting_identity_is_not_spliced(identity):
    base = {**_source(), "doi": "10.fixture/current"}
    foreign = {**base, **identity, "deep_read_material": {"finding": "FOREIGN-FINDING"}}
    rows, report = planning._resolve_owner_source_materials(
        source_materials=[base], chapter_plan=_plan(), candidate_materials=[foreign])
    assert report["unresolved"][0]["reason"] == "source_identity_conflict"
    assert rows[0]["material_identity_conflict"] is True
    assert "FOREIGN-FINDING" not in json.dumps(rows)
    assert not planning._owner_material_has_content(rows[0])


def test_duplicate_source_rows_preserve_complements_but_reject_conflicting_identity():
    base = _source()
    extra = {**base, "deep_read_material": {"finding": "DUPLICATE-COMPLEMENT"}}
    rows, report = planning._resolve_owner_source_materials(source_materials=[base, extra], chapter_plan=_plan())
    assert "DUPLICATE-COMPLEMENT" in json.dumps(rows)
    assert not report["unresolved"]
    rows, report = planning._resolve_owner_source_materials(
        source_materials=[base, {**extra, "paper_id": "foreign"}], chapter_plan=_plan())
    assert rows[0]["paper_id"] == base["paper_id"]
    assert "DUPLICATE-COMPLEMENT" not in json.dumps(rows)
    assert report["unresolved"][0]["reason"] == "source_identity_conflict"


def test_nested_empty_answers_do_not_become_study_material():
    empty = {"source_handle": "P0101", "paper_id": "original", "deep_read_material": {
        "question_material": [{"question_id": "Q1", "explanation": "", "examples": [], "remaining_points": ["unknown"]}]}}
    payload = _payload(candidate_materials=[{**_source("P0100"), "sources": [empty]}])
    result, _seen = owner_run(payload, [{"access_id": "candidate_materials[0].sources[0]"}], ["P0101"])
    assert result["status"] == "unresolved"
    assert "updated_unit_sources_unavailable:P0101" in result["structural_errors"]


def test_batch_adoption_accumulates_across_resume_and_later_uncited_sources(tmp_path):
    payload = navigation_payload(4)
    payload["candidate_materials"] = payload["source_materials"][2:]
    payload["source_materials"] = payload["source_materials"][:2]
    for row in [*payload["source_materials"], *payload["candidate_materials"]]:
        row["usable_content"] += "Q" * 12000
    payload["input_integrity"] = strengthening._input_integrity(payload)
    catalog = demand.build_material_catalog(payload)
    requests = [{"access_id": row["access_id"]} for row in catalog["entries"]]
    trace = demand.resolve_material_requests(payload, catalog, {"status": "access_plan", "material_requests": requests})
    counter = lambda _raw, messages: max(1, sum(len(row["content"]) for row in messages) // 100)
    profile = {"model": "qwen3.8-max", "thinking_budget": 8, "max_output_tokens": 64, "json_mode": False}
    empty = {**trace, "selected_materials": [], "resolved_count": 0}
    capacity = strengthening.estimate_strengthening_request(
        demand.owner_messages(payload, catalog, empty), profile=profile, token_counter=counter)["total_context_tokens"] + 200
    seen = []
    calls = {"access": 0, "owner": 0}
    fail = [True]

    def access(*_args, **_kwargs):
        calls["access"] += 1
        return raw({"status": "access_plan", "material_requests": requests})

    def owner(messages, **kwargs):
        calls["owner"] += 1
        text = messages[-1]["content"]
        visible = json.JSONDecoder().raw_decode(text[text.index("{"):])[0]
        handles = [row["source_handle"] for row in visible["candidate_materials"]]
        if "P0003" in handles and fail[0]:
            raise RuntimeError("pause_after_candidate_adoption")
        seen.append({"call_id": kwargs["call_id"], "handles": handles, "plan": deepcopy(visible["chapter_plan"])})
        plan = visible["chapter_plan"]
        if handles:
            # Later work replaces the earlier candidate's citation. Its accepted
            # evidence should stay in the material pool for arrangement/reuse.
            plan["units"][0]["source_handles"] = ["P0000", *handles]
        plan["units"][0]["substantive_point"] += " | bounded batch"
        return raw({"status": "updated", "updated_plan": plan})

    args = {**ARGS, "access_max_output_tokens": 64, "owner_max_output_tokens": 64,
            "access_client": access, "owner_client": owner, "owner_profile": profile,
            "owner_token_counter": counter, "owner_context_limit_tokens": capacity,
            "checkpoint_dir": tmp_path, "resume": True}
    with pytest.raises(RuntimeError, match="pause_after_candidate_adoption"):
        demand.run_on_demand_strengthening(payload, **args)
    progress = json.loads((tmp_path / "MATERIAL_BATCH_PROGRESS.json").read_text())
    assert "P0002" in {row["source_handle"] for row in progress["accepted_source_materials"]}
    fail[0] = False
    result = demand.run_on_demand_strengthening(payload, **args)
    assert result["status"] == "updated", result
    assert result["material_batch_count"] > 1
    assert calls["access"] == 1
    assert sum("P0002" in row["handles"] for row in seen) == 1
    assert "P0002" not in planning._owner_referenced_source_handles(result["updated_plan"])
    assert {row["source_handle"] for row in result["accepted_source_materials"]} == {"P0000", "P0001", "P0002", "P0003"}
    projected = strengthening.project_plan_for_arrangement(payload, result)
    assert {row["source_handle"] for row in projected["source_materials"]} == {"P0000", "P0001", "P0002", "P0003"}
    assert result["material_batch_results"][0]["result"]["on_demand_strengthening"]["owner_reused"]
    before = dict(calls)
    replay = demand.run_on_demand_strengthening(payload, **args)
    assert calls == before
    assert replay["accepted_source_materials"] == result["accepted_source_materials"]


def test_current_pool_corrections_replace_superseded_material_not_add_variants(monkeypatch):
    base = _source()
    corrected = _source()
    families = {
        "study_summary_A": "study_summary_A_variants", "review_planning_B": "review_planning_B_variants",
        "deep_read_material": "deep_read_materials", "supplement_material": "supplement_materials",
        "local_passages": "local_passages_variants",
    }
    for singular, plural in families.items():
        base[singular] = {"finding": "SUPERSEDED-" + singular}
        base[plural] = [{"finding": "SUPERSEDED-VARIANT-" + singular}]
        corrected[singular] = {"finding": "CORRECTED-" + singular}
    base["supplement_gap_material"] = {"finding": "SUPERSEDED-GAP-ALIAS"}
    base["supplement_gap_materials"] = [{"finding": "SUPERSEDED-GAP-VARIANT"}]
    monkeypatch.setattr(planning, "build_local_material_payload", lambda *_args, **_kwargs: deepcopy(corrected))
    rows, report = planning._resolve_owner_source_materials(
        source_materials=[base], chapter_plan=_plan(), candidate_materials=[deepcopy(base)],
        pool_rows=[{"_source_handle": "P0001", "_paper_id": base["paper_id"]}])
    assert not report["unresolved"]
    assert "SUPERSEDED" not in json.dumps(rows)
    for singular, plural in families.items():
        assert rows[0][singular] == corrected[singular]
        assert not rows[0].get(plural)


def test_current_pool_empty_cards_do_not_erase_other_usable_material(monkeypatch):
    base = {**_source(), "deep_read_material": {"finding": "RETAIN-DEEP-READ"}}
    corrected = {**_source(), "study_summary_A": {"finding": "CORRECTED-A"}, "deep_read_material": {}}
    monkeypatch.setattr(planning, "build_local_material_payload", lambda *_args, **_kwargs: deepcopy(corrected))
    rows, report = planning._resolve_owner_source_materials(
        source_materials=[base], chapter_plan=_plan(),
        pool_rows=[{"_source_handle": "P0001", "_paper_id": base["paper_id"]}])
    assert not report["unresolved"]
    assert rows[0]["deep_read_material"] == base["deep_read_material"]
    assert rows[0]["study_summary_A"] == corrected["study_summary_A"]
    assert "Observed finding P0001" not in json.dumps(rows)


@pytest.mark.parametrize("nested_key", ["sources", "source_materials", "candidate_materials", "tool_supplement_materials"])
def test_tool_nested_full_original_uses_same_adoption_contract(tmp_path, nested_key):
    original = {"source_handle": "P0101", "paper_id": "reported-original", "title": "Reported study",
                "deep_read_material": {"finding": "TOOL-NESTED-FINDING", "conditions": "TOOL-NESTED-CONDITION"}}
    container = {**_source("P0100"), nested_key: [original]}
    payload = _payload(tool_materials=[container])
    result, seen = owner_run(payload, [{"access_id": f"tool_materials[0].{nested_key}[0]"}], ["P0101"])
    assert "TOOL-NESTED-CONDITION" in json.dumps(seen)
    assert result["status"] == "updated", result["structural_errors"]
    accepted = next(row for row in result["accepted_source_materials"] if row["source_handle"] == "P0101")
    assert accepted["deep_read_material"] == original["deep_read_material"]
    assert accepted["paper_id"] == original["paper_id"]
    assert not accepted.get("study_summary_A")
    assert "TOOL-NESTED-CONDITION" in json.dumps(downstream_view(tmp_path, payload, result).to_dict())
