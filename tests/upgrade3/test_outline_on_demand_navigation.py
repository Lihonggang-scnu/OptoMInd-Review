"""Offline progressive material-navigation and resolver contracts."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import outline_on_demand as on_demand
from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
from optomind_research.runtime.upgrade3 import progressive_review_plan as progressive


def _payload(count: int = 8):
    rows = []
    for index in range(count):
        rows.append({
            "source_handle": f"P{index:04d}",
            "paper_id": f"paper-{index}",
            "doi": f"10.1000/{index}",
            "title": f"Study {index}",
            "study_summary_A": {
                "key_findings": [{"finding": f"FINDING-{index}", "conditions": f"CONDITION-{index}"}],
                "limits": [f"LIMIT-{index}"],
            },
            "review_planning_B": {"planning_summary": f"PLANNING-{index}"},
            "usable_content": f"NEGATIVE-MARKER-{index}",
        })
    tool = {
        "source_handle": "P0000",
        "paper_id": "paper-0",
        "usable_content": "TOOL-COMPLEMENT",
        "conditions": "TOOL-CONDITION",
    }
    return strengthening.build_strengthening_payload(
        research_question="How do supplied studies support a bounded comparison?",
        chapter_id="CH02",
        chapter_plan={"units": [
            {"unit_id": "CH02_U01", "substantive_point": "Use P0000", "source_handles": ["P0000"]},
            {"unit_id": "CH02_U03", "substantive_point": "Preserve boundary", "source_handles": ["P0001"]},
        ]},
        source_materials=rows,
        tool_materials=[tool],
        readonly_neighbor_unit_roles=[{"unit_id": "CH02_U02", "role": "read-only boundary"}],
        full_chapter_context={"unit_order": ["CH02_U01", "CH02_U02", "CH02_U03"]},
        modifiable_unit_ids=["CH02_U01", "CH02_U03"],
        read_only_unit_ids=["CH02_U02"],
        call_id="offline-navigation",
    )


def _resolve(payload, catalog, requests):
    return on_demand.resolve_material_requests(
        payload, catalog, {"status": "access_plan", "material_requests": requests},
    )


def test_navigation_keeps_all_identity_entries_but_only_priority_semantics():
    payload = _payload()
    catalog = on_demand.build_material_catalog(payload)
    view = on_demand.navigation_catalog(catalog, payload, page_size=2)
    assert view["entry_count"] == len(catalog["entries"])
    assert view["entry_count"] == 9
    assert view["priority_entry_count"] >= 1
    assert all("identity" in row and row["full_record_available"] for row in view["entries"])
    assert all("source_supplied_locators" not in row for row in view["entries"])
    assert any("CONDITION-0" in json.dumps(row, ensure_ascii=False) for row in view["priority_semantic_entries"])
    assert view["semantic_page"]["next_page_request"]["request_type"] == "catalog_page"


def test_actual_access_messages_are_stable_across_process_hash_seeds():
    code = '''
import runpy, hashlib, json
from optomind_research.runtime.upgrade3 import outline_on_demand as od
p = runpy.run_path("tests/upgrade3/test_outline_on_demand_navigation.py")["_payload"]()
m = od.access_messages(p, od.build_material_catalog(p))
print(hashlib.sha256(json.dumps(m, ensure_ascii=False, sort_keys=True).encode()).hexdigest())
'''
    hashes = [subprocess.check_output(
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "PYTHONHASHSEED": seed}, text=True,
    ).strip() for seed in ("1", "2", "3")]
    assert len(set(hashes)) == 1


def test_page_and_search_requests_expand_complete_original_records():
    payload = _payload()
    catalog = on_demand.build_material_catalog(payload)
    page = _resolve(payload, catalog, [{"request_type": "catalog_page", "page": 1, "page_size": 2}])
    assert page["resolved_count"] == 2
    assert all("study_summary_A" in row["record"] for row in page["selected_materials"])
    searched = _resolve(payload, catalog, [{"request_type": "catalog_search", "query": "NEGATIVE-MARKER-6", "limit": 2}])
    assert searched["resolved_count"] == 1
    assert searched["selected_materials"][0]["record"]["usable_content"] == "NEGATIVE-MARKER-6"
    paged = _resolve(payload, catalog, [{"request_type": "catalog_search", "query": "NEGATIVE-MARKER", "offset": 2, "limit": 2}])
    assert paged["trace"][0]["catalog_total_matches"] == 8
    assert paged["trace"][0]["catalog_remaining_count"] == 4
    assert paged["trace"][0]["catalog_next_request"]["offset"] == 4
    mixed = _resolve(payload, catalog, [
        {"request_type": "catalog_search", "query": "NO-SUCH-RECORD", "limit": 2},
        {"request_type": "catalog_search", "query": "NEGATIVE-MARKER-1", "limit": 1},
    ])
    assert mixed["trace"][0]["catalog_no_matches"] is True
    assert mixed["resolved_count"] == 1


def test_owner_sees_selected_full_record_and_remaining_light_navigation_only():
    payload = _payload()
    catalog = on_demand.build_material_catalog(payload)
    trace = _resolve(payload, catalog, [{"request_type": "catalog_search", "query": "NEGATIVE-MARKER-6", "limit": 1}])
    messages = on_demand.owner_messages(payload, catalog, trace)
    rendered = json.dumps(messages, ensure_ascii=False)
    assert "NEGATIVE-MARKER-6" in rendered
    assert "study_summary_A" in rendered
    # A different unselected row remains addressable by identity, but its
    # long semantic fields are not silently sent to the owner.
    assert "NEGATIVE-MARKER-7" not in rendered
    owner_view = on_demand._model_visible_payload(on_demand._owner_payload(payload, catalog, trace), catalog, trace)
    entries = owner_view["on_demand_material_access"]["catalog"]["entries"]
    selected = [row for row in entries if row.get("material_ref")]
    assert selected and all(row.get("full_record_in_request") for row in selected)
    assert all("source_supplied_locators" not in row for row in entries)


def test_complementary_tool_alias_is_preserved_with_source_record():
    payload = _payload()
    catalog = on_demand.build_material_catalog(payload)
    trace = _resolve(payload, catalog, [{"source_handle": "P0000", "unit_ids": ["CH02_U01"]}])
    assert {row["channel"] for row in trace["selected_materials"]} == {"source_materials", "tool_materials"}
    assert "TOOL-COMPLEMENT" in json.dumps(trace["selected_materials"], ensure_ascii=False)


def test_readonly_unit_request_expands_role_without_edit_scope_leak():
    payload = _payload()
    catalog = on_demand.build_material_catalog(payload)
    trace = _resolve(payload, catalog, [{"request_type": "readonly_unit", "unit_id": "CH02_U02"}])
    assert trace["resolved_count"] == 0
    assert trace["readonly_context_reads"][0]["unit_id"] == "CH02_U02"
    owner = on_demand.owner_messages(payload, catalog, trace)
    assert "read-only boundary" in json.dumps(owner, ensure_ascii=False)
    with pytest.raises(on_demand.OnDemandMaterialError, match="readonly_unit_is_editable"):
        _resolve(payload, catalog, [{"request_type": "readonly_unit", "unit_id": "CH02_U01"}])


def test_no_change_does_not_trigger_material_read():
    payload = _payload()
    catalog = on_demand.build_material_catalog(payload)
    validated = on_demand.validate_access_response({"status": "no_change"}, catalog)
    assert validated["material_requests"] == []


def test_full_record_hash_survives_page_resume_without_truncation():
    payload = _payload(3)
    catalog = on_demand.build_material_catalog(payload)
    first = _resolve(payload, catalog, [{"request_type": "catalog_page", "page": 0, "page_size": 1}])
    second = _resolve(
        payload, catalog,
        [{"request_type": "catalog_page", "page": 1, "page_size": 1}],
    )
    for item in [*first["selected_materials"], *second["selected_materials"]]:
        assert hashlib.sha256(json.dumps(item["record"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == item["record"].get("record_sha256", on_demand._hash(item["record"]))


def test_paid_access_resume_does_not_call_access_twice(tmp_path):
    payload = _payload(3)
    calls = {"access": 0, "owner": 0}

    def access_client(messages, **kwargs):
        calls["access"] += 1
        return {"content": json.dumps({"status": "no_change", "material_requests": []}), "complete": True, "finish_reason": "stop", "usage": {"prompt_tokens": 4, "completion_tokens": 2}}

    def blocked_owner(messages, **kwargs):
        calls["owner"] += 1
        raise RuntimeError("budget_guard_pause")

    common = dict(
        access_client=access_client, access_model="qwen3.5-plus", access_thinking_budget=8, access_max_output_tokens=8,
        owner_model="qwen3.8-max", owner_thinking_budget=8, owner_max_output_tokens=8,
        checkpoint_dir=tmp_path / "stages", resume=True,
    )
    with pytest.raises(RuntimeError, match="budget_guard_pause"):
        on_demand.run_on_demand_strengthening(payload, owner_client=blocked_owner, **common)
    assert calls == {"access": 1, "owner": 1}
    assert (tmp_path / "stages" / "ACCESS_STAGE.json").is_file()

    def owner_resume(messages, **kwargs):
        calls["owner"] += 1
        return {"content": json.dumps({"status": "no_change", "chapter_updates": []}), "complete": True, "finish_reason": "stop", "usage": {"prompt_tokens": 4, "completion_tokens": 2}}

    def access_must_not_repeat(messages, **kwargs):
        raise AssertionError("paid access must be resumed from its saved stage")

    resumed = on_demand.run_on_demand_strengthening(
        payload, **{**common, "access_client": access_must_not_repeat, "owner_client": owner_resume},
    )
    assert resumed["status"] == "no_change"
    assert calls == {"access": 1, "owner": 2}


def test_normal_runner_no_extra_access_call(tmp_path):
    payload = _payload(2)
    calls = []

    def access_client(messages, **kwargs):
        calls.append("access")
        return {"content": json.dumps({"status": "no_change", "material_requests": []}), "complete": True, "finish_reason": "stop", "usage": {"prompt_tokens": 4, "completion_tokens": 2}}

    def owner_client(messages, **kwargs):
        calls.append("owner")
        return {"content": json.dumps({"status": "no_change", "chapter_updates": []}), "complete": True, "finish_reason": "stop", "usage": {"prompt_tokens": 4, "completion_tokens": 2}}

    result = on_demand.run_on_demand_strengthening(
        payload, access_client=access_client, access_model="qwen3.5-plus", access_thinking_budget=8, access_max_output_tokens=8,
        owner_client=owner_client, owner_model="qwen3.8-max", owner_thinking_budget=8, owner_max_output_tokens=8,
        checkpoint_dir=tmp_path / "stages", resume=True,
    )
    assert result["status"] == "no_change"
    assert calls == ["access", "owner"]


def test_oversize_complete_record_batches_preserve_all_rows():
    payload = _payload(7)
    rows = [dict(row) for row in payload["source_materials"]]
    weights = [len(json.dumps(row, ensure_ascii=False)) for row in rows]
    partitions = progressive._chapter_details_partitions(rows, weights, 3)
    assert sum(len(part) for part in partitions) == len(rows)
    original = {on_demand._hash(row) for row in rows}
    rebuilt = []
    for index, part in enumerate(partitions, start=1):
        batch = progressive._chapter_details_batch_payload(payload, part, index=index, count=len(partitions))
        assert batch["chapter_details_batch"]["complete_source_records"] is True
        rebuilt.extend(batch["source_materials"])
    assert {on_demand._hash(row) for row in rebuilt} == original



def test_actual_owner_overflow_batches_and_resumes_successful_calls(tmp_path):
    payload = _payload(4)
    for row in payload["source_materials"]:
        row["usable_content"] += "Q" * 12000
    payload["input_integrity"] = strengthening._input_integrity(payload)
    catalog = on_demand.build_material_catalog(payload)
    requests = [{"access_id": row["access_id"]} for row in catalog["entries"]]
    trace = _resolve(payload, catalog, requests)
    counter = lambda raw, messages: max(1, sum(len(row["content"]) for row in messages) // 100)
    empty = {**trace, "selected_materials": [], "resolved_count": 0}
    profile = {"model": "qwen3.8-max", "thinking_budget": 8, "max_output_tokens": 64, "json_mode": False}
    base = strengthening.estimate_strengthening_request(on_demand.owner_messages(payload, catalog, empty), profile=profile, token_counter=counter)["total_context_tokens"]
    capacity = base + 190
    assert strengthening.estimate_strengthening_request(on_demand.owner_messages(payload, catalog, trace), profile=profile, token_counter=counter)["total_context_tokens"] > capacity
    calls = {"access": 0, "owner": 0}
    seen = []
    fail = [True]

    def access(messages, **kwargs):
        calls["access"] += 1
        return {"content": json.dumps({"status": "access_plan", "material_requests": requests}), "finish_reason": "stop", "complete": True}

    def owner(messages, **kwargs):
        calls["owner"] += 1
        if calls["owner"] == 2 and fail[0]:
            raise RuntimeError("budget_pause_after_successful_batch")
        estimate = strengthening.estimate_strengthening_request(messages, profile=profile, token_counter=counter)
        assert estimate["total_context_tokens"] <= capacity
        content = messages[1]["content"]
        visible = json.JSONDecoder().raw_decode(content[content.index("{"):])[0]
        seen.extend(row["paper_id"] for row in visible["source_materials"])
        assert all(len(row["usable_content"]) > 12000 for row in visible["source_materials"])
        if calls["owner"] == 1:
            plan = visible["chapter_plan"]
            plan["units"][0]["substantive_point"] += " | revised responsibility"
            response = {"status": "updated", "chapter_updates": [{"chapter_id": "CH02", "updated_plan": plan}]}
        else:
            assert "revised responsibility" in visible["chapter_plan"]["units"][0]["substantive_point"]
            response = {"status": "no_change", "chapter_updates": []}
        return {"content": json.dumps(response), "complete": True, "finish_reason": "stop"}

    args = dict(access_client=access, access_model="qwen3.5-plus", access_thinking_budget=8, access_max_output_tokens=64,
                owner_client=owner, owner_model="qwen3.8-max", owner_thinking_budget=8, owner_max_output_tokens=64,
                owner_profile=profile, owner_token_counter=counter, owner_context_limit_tokens=capacity,
                checkpoint_dir=tmp_path, resume=True)
    with pytest.raises(RuntimeError, match="budget_pause"):
        on_demand.run_on_demand_strengthening(payload, **args)
    assert (tmp_path / "material_batch_001" / "OWNER_STAGE.json").is_file()
    fail[0] = False
    result = on_demand.run_on_demand_strengthening(payload, **args)
    assert result["status"] == "updated"
    assert "revised responsibility" in result["updated_plan"]["units"][0]["substantive_point"]
    assert result["material_batch_count"] > 1
    assert calls["access"] == 1
    assert len(seen) == 4 and set(seen) == {"paper-0", "paper-1", "paper-2", "paper-3"}
    assert result["material_batch_results"][0]["result"]["on_demand_strengthening"]["owner_reused"]
    assert len(result["on_demand_strengthening"]["material_access_trace"]["selected_materials"]) == len(catalog["entries"])
