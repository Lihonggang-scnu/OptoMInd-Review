"""WO-01: real adaptive cache/recovery paths with model/tokenizer boundaries faked."""

from __future__ import annotations

import copy
import json

import pytest

from optomind_research.runtime.upgrade3 import progressive_review_plan as planning


class BoundaryCounter:
    def __call__(self, _encoding, messages):
        content = messages[-1]["content"]
        if "当前任务：chapter_details\n" in content:
            content = content.split("当前任务：chapter_details\n", 1)[1].split("\n\n【本轮交付】", 1)[0]
        value = json.loads(content)
        if "source_handle" in value:
            return 150_000
        return 100_000 + 150_000 * len(value.get("source_materials") or [])


_DEFAULT = object()


class BoundaryPlanner:
    counter = BoundaryCounter()
    output_tokens = 16_000

    def __init__(self, *, merge=_DEFAULT, batches=None):
        self.merge = merge
        self.batches = batches or {}
        self.calls = []

    def __call__(self, stage, payload):
        self.calls.append((stage, copy.deepcopy(dict(payload))))
        is_merge = "chapter_detail_batches" in payload
        result = self.merge if is_merge else self.batches.get(payload.get("chapter_details_batch", {}).get("index"), _DEFAULT)
        if isinstance(result, Exception):
            raise result
        if result is _DEFAULT:
            handles = ([handle for batch in payload["chapter_detail_batches"] for handle in batch["source_handles"]]
                       if is_merge else [row["source_handle"] for row in payload["source_materials"]])
            result = {"chapter_plan": {
                "thesis": "Supplied findings", "reader_objective": "Explain the findings",
                "source_handles": handles,
                "units": [{"substantive_point": "Describe supplied findings", "source_handles": handles,
                           "paragraph_briefs": [{"point": "Explain the finding", "development": "Keep its conditions",
                                                  "source_handles": handles}]}],
            }}
        if result.get("_planner_call"):
            return copy.deepcopy(result)
        return {"_planner_call": True, "response": copy.deepcopy(result), "telemetry": {"finish_reason": "stop"}}


def _payload(count=6):
    return {"topic_id": "batch-contract", "research_question": "Supplied question",
            "chapter": {"chapter_id": "CH01", "title": "Supplied chapter"},
            "source_materials": [{"source_handle": f"P{index:04d}", "study_summary_A": {"finding": "A"},
                                  "planning_material": {"finding": "B"}} for index in range(count)],
            "candidate_materials": []}


def _run(planner, root, *, resume=False, payload=None):
    return planning._chapter_details_adaptive_record(
        planner, payload or _payload(), chapter_id="CH01", cache_root=root, resume=resume,
    )


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("invalid", [
    {}, {"chapter_plan": {}}, {"chapter_plan": {"thesis": "Only a heading"}},
    {"chapter_plan": {"units": []}}, {"chapter_plan": {"units": [{}]}},
    {"chapter_plan": {"units": [{"unit_id": "U1", "source_handles": ["P0000"]}]}},
    {"status": "failed", "chapter_plan": {"units": [{"substantive_point": "Unfinished"}]}},
    {"chapter_plan": {"status": "partial", "units": [{"substantive_point": "Unfinished"}]}},
    {"_planner_call": True, "response": {"chapter_plan": {"units": [{"substantive_point": "Cut off"}]}},
     "telemetry": {"complete": False}},
    {"_planner_call": True, "response": {"chapter_plan": {"units": [{"substantive_point": "Cut off"}]}},
     "telemetry": {"finish_reason": "length"}},
])
def test_invalid_merge_persists_failed_seam_and_reuses_successful_batches(tmp_path, invalid):
    with pytest.raises(planning.ProgressivePlanError, match="chapter_details_merge_"):
        _run(BoundaryPlanner(merge=invalid), tmp_path)

    batches = list(tmp_path.glob("batch_*.json"))
    assert len(batches) == 2
    assert all(_read(path)["status"] == "complete" for path in batches)
    assert not list(tmp_path.glob("merge_*.json"))
    failure = _read(next((tmp_path / "failed_seams").glob("merge_*.json")))
    assert failure["status"] == "failed"
    assert failure["seam"] == "merge"
    assert len(failure["successful_batches"]) == 2
    assert failure["record"]["response"] == (invalid["response"] if invalid.get("_planner_call") else invalid)

    resumed = BoundaryPlanner()
    _record, meta = _run(resumed, tmp_path, resume=True)
    assert len(resumed.calls) == 1
    assert "chapter_detail_batches" in resumed.calls[0][1]
    assert all(batch["reused"] for batch in meta["batches"])
    assert _read(next(tmp_path.glob("merge_*.json")))["status"] == "complete"


def test_transport_failure_records_batch_seam_and_resumes_only_missing_work(tmp_path):
    with pytest.raises(RuntimeError, match="Interrupted batch"):
        _run(BoundaryPlanner(batches={2: RuntimeError("Interrupted batch")}), tmp_path)

    first_path = next(tmp_path.glob("batch_*.json"))
    first_bytes = first_path.read_bytes()
    failure = _read(next((tmp_path / "failed_seams").glob("batch_*.json")))
    assert failure["status"] == "failed"
    assert failure["batch_index"] == 2
    assert failure["error"] == "Interrupted batch"
    assert failure["error_type"] == "RuntimeError"
    assert len(failure["successful_batches"]) == 1
    assert not list(tmp_path.glob("merge_*.json"))

    resumed = BoundaryPlanner()
    _record, meta = _run(resumed, tmp_path, resume=True)
    assert len(resumed.calls) == 2
    assert resumed.calls[0][1]["chapter_details_batch"]["index"] == 2
    assert meta["batches"][0]["reused"] is True
    assert first_path.read_bytes() == first_bytes


def test_failed_batch_response_is_not_cached_as_complete_or_sent_to_merge(tmp_path):
    planner = BoundaryPlanner(batches={2: {"status": "failed", "chapter_plan": {"units": []}}})
    with pytest.raises(planning.ProgressivePlanError, match="chapter_details_batch_"):
        _run(planner, tmp_path)
    assert len(planner.calls) == 2
    assert len(list(tmp_path.glob("batch_*.json"))) == 1
    failure = _read(next((tmp_path / "failed_seams").glob("batch_*.json")))
    assert failure["record"]["response"]["status"] == "failed"
    assert failure["batch_index"] == 2


def test_merge_transport_failure_keeps_batches_and_records_recovery_seam(tmp_path):
    with pytest.raises(RuntimeError, match="Interrupted merge"):
        _run(BoundaryPlanner(merge=RuntimeError("Interrupted merge")), tmp_path)
    failure = _read(next((tmp_path / "failed_seams").glob("merge_*.json")))
    assert failure["error_type"] == "RuntimeError"
    assert len(failure["successful_batches"]) == 2
    assert not list(tmp_path.glob("merge_*.json"))
    resumed = BoundaryPlanner()
    _run(resumed, tmp_path, resume=True)
    assert len(resumed.calls) == 1
    assert "chapter_detail_batches" in resumed.calls[0][1]


def test_merge_capacity_failure_keeps_batches_without_calling_merge(tmp_path):
    class HighMergeCounter(BoundaryCounter):
        def __call__(self, encoding, messages):
            if '"chapter_detail_batches":' in messages[-1]["content"]:
                return 1_000_000
            return super().__call__(encoding, messages)

    planner = BoundaryPlanner()
    planner.counter = HighMergeCounter()
    with pytest.raises(planning.ProgressivePlanError, match="merge_context_preflight_exceeded"):
        _run(planner, tmp_path)
    assert len(planner.calls) == 2
    failure = _read(next((tmp_path / "failed_seams").glob("merge_*.json")))
    assert failure["estimate"]["fits"] is False
    assert len(failure["successful_batches"]) == 2
    resumed = BoundaryPlanner()
    _run(resumed, tmp_path, resume=True)
    assert len(resumed.calls) == 1


def test_supported_substantive_units_alias_reuses_valid_cache(tmp_path):
    alias = {"chapter_plan": {"substantive_units": [{"substantive_point": "Supplied finding"}]}}
    _run(BoundaryPlanner(merge=alias, batches={1: alias, 2: alias}), tmp_path)
    resumed = BoundaryPlanner()
    record, meta = _run(resumed, tmp_path, resume=True)
    assert resumed.calls == []
    assert meta["merge_reused"] is True
    assert record["response"] == alias


@pytest.mark.parametrize("seam", ["batch", "merge"])
def test_failed_forced_attempt_does_not_overwrite_compatible_success(tmp_path, seam):
    _run(BoundaryPlanner(), tmp_path)
    saved = {path: path.read_bytes() for path in tmp_path.glob("*.json")}
    invalid = {"chapter_plan": {"units": []}}
    planner = BoundaryPlanner(merge=invalid) if seam == "merge" else BoundaryPlanner(batches={2: invalid})
    with pytest.raises(planning.ProgressivePlanError, match=f"chapter_details_{seam}_"):
        _run(planner, tmp_path)
    assert all(path.read_bytes() == content for path, content in saved.items())
    assert _read(next((tmp_path / "failed_seams").glob(f"{seam}_*.json")))["status"] == "failed"
    resumed = BoundaryPlanner()
    _record, meta = _run(resumed, tmp_path, resume=True)
    assert resumed.calls == []
    assert meta["merge_reused"] is True


@pytest.mark.parametrize("seam", ["batch", "merge"])
def test_legacy_complete_cache_with_empty_units_is_revalidated(tmp_path, seam):
    _run(BoundaryPlanner(), tmp_path)
    path = next(tmp_path.glob(f"{seam}_*.json"))
    invalid = _read(path)
    invalid["record"]["response"]["chapter_plan"]["units"] = []
    path.write_text(json.dumps(invalid), encoding="utf-8")
    resumed = BoundaryPlanner()
    _run(resumed, tmp_path, resume=True)
    assert len(resumed.calls) == 1
    assert bool(resumed.calls[0][1].get("chapter_detail_batches")) == (seam == "merge")
    assert _read(path)["record"]["response"]["chapter_plan"]["units"]


def test_unsplit_path_retains_original_response_and_creates_no_batch_cache(tmp_path):
    planner = BoundaryPlanner(batches={None: {"chapter_plan": {"units": []}}})
    payload = _payload(count=1)
    before = copy.deepcopy(payload)
    record, meta = _run(planner, tmp_path / "not_created", payload=payload)
    assert record["response"] == {"chapter_plan": {"units": []}}
    assert meta is None
    assert planner.calls == [("chapter_details", before)]
    assert payload == before
    assert not (tmp_path / "not_created").exists()


def test_real_chapter_writer_preserves_prior_packet_when_adaptive_merge_fails(tmp_path):
    config = planning.ProgressivePlannerConfig(
        topic_id="batch-contract", pool_path=tmp_path / "unused_pool.jsonl", plan_path=tmp_path / "unused_plan.json",
        output_dir=tmp_path / "run", chapter_workers=1,
    )
    planner = planning.ProgressiveReviewPlanner(config, planner=BoundaryPlanner())
    candidates = {f"paper-{index}": {"_source_handle": f"P{index:04d}", "_paper_id": f"paper-{index}",
                                    "planning_view": {"paper_identity": {"canonical_paper_id": f"paper-{index}"},
                                                      "planning_summary": "Supplied finding"}}
                  for index in range(6)}
    kwargs = {"chapters": [{"chapter_id": "CH01", "title": "Supplied chapter", "source_ids": list(candidates)}],
              "shared_outline": {}, "topic": "Supplied question", "candidates": candidates,
              "level1_tools": {}, "level2_tools": {}, "resume": False, "state": {}}
    planner._chapter_details(**kwargs)
    packet_path = config.output_dir / "stages" / "chapters" / "CH01.json"
    before = packet_path.read_bytes()
    planner.planner = BoundaryPlanner(merge={"chapter_plan": {"units": []}})
    with pytest.raises(planning.ProgressivePlanError, match="chapter_details_merge_"):
        planner._chapter_details(**kwargs)
    assert packet_path.read_bytes() == before
    failure = next((packet_path.parent / "CH01_details_batches" / "failed_seams").glob("merge_*.json"))
    assert _read(failure)["status"] == "failed"
