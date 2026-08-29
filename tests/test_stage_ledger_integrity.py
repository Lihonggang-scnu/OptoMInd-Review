"""Regression tests for the stage ledgers, from the rhr_be780761 post-mortem.

Two defects found on that run are pinned here:

* ``HARNESS_STATE.json`` grew to 9,077,142 B because the ``topic_scoped_kb``
  stage entry carried its full evidence/selection payload (5,555,789 B for that
  one key).  The file is rewritten on every stage transition and re-read by the
  UI on every poll, so the cost was paid many times over.
* Six stages emitted ``stage_finished`` with no matching ``stage_started`` and
  therefore recorded ``wall_time_seconds: 0.0``.  Two of them had *billed real
  money*: ``publication_mainline_commander`` 0.600954 CNY over 955,104 input
  tokens and ``publication_mainline_staged_completion`` 0.661369 CNY over
  525,857 tokens.  A zero duration was indistinguishable from an instant stage.
"""

from __future__ import annotations

import json
from pathlib import Path

from optomind_research.runtime.harness_observability import HarnessObservability
from optomind_research.runtime.review_harness_orchestrator import (
    _compact_stage_detail,
)


def _events(work_dir: Path) -> list[dict]:
    path = work_dir / "HARNESS_EVENTS.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestStageDetailCompaction:
    """#46 -- bulk payloads must not reach the rewritten-every-time state file."""

    def test_long_lists_become_count_plus_sample(self):
        detail = {"rows": [{"chunk": n} for n in range(14_033)]}

        compact = _compact_stage_detail(detail)

        assert compact["rows_count"] == 14_033
        assert len(compact["rows_sample"]) == 20
        assert compact["rows_sample"][0] == {"chunk": 0}
        # The bulk key itself must be gone, not merely shortened in place.
        assert "rows" not in compact

    def test_nested_payloads_are_compacted_at_every_depth(self):
        detail = {
            "evidence": {"rows": [{"chunk": n} for n in range(14_033)]},
            "selection": {
                "papers": [{"paper_id": n} for n in range(375)],
                "qualified_paper_ids": list(range(375)),
            },
        }

        compact = _compact_stage_detail(detail)

        assert compact["evidence"]["rows_count"] == 14_033
        assert compact["selection"]["papers_count"] == 375
        assert compact["selection"]["qualified_paper_ids_count"] == 375
        assert len(compact["selection"]["qualified_paper_ids_sample"]) == 20

    def test_nothing_is_dropped_without_a_count(self):
        """A reader must always be able to tell how much was elided."""
        detail = {"rows": [{"chunk": n} for n in range(100)]}

        compact = _compact_stage_detail(detail)

        assert compact["rows_count"] == len(detail["rows"])

    def test_short_lists_and_scalars_pass_through_untouched(self):
        detail = {
            "reused": True,
            "error": "",
            "queries": ["a", "b", "c"],
            "count": 3,
        }

        assert _compact_stage_detail(detail) == detail

    def test_boundary_at_the_sample_limit_is_not_compacted(self):
        detail = {"rows": list(range(20))}

        compact = _compact_stage_detail(detail)

        assert compact["rows"] == list(range(20))
        assert "rows_count" not in compact

    def test_non_mapping_input_is_returned_as_is(self):
        assert _compact_stage_detail("scoped") == "scoped"
        assert _compact_stage_detail(None) is None

    def test_compaction_shrinks_a_realistic_payload_by_orders_of_magnitude(self):
        """The point of the fix is size, so measure size.

        Shape mirrors the real ``topic_scoped_kb`` entry: 14,033 evidence rows
        and 375 selected papers, which serialised to 5,555,789 B.
        """

        detail = {
            "evidence": {
                "rows": [
                    {
                        "chunk_id": f"c{n}",
                        "paper_id": f"p{n % 375}",
                        "text": "lorem ipsum dolor sit amet " * 8,
                    }
                    for n in range(14_033)
                ],
            },
            "selection": {
                "papers": [
                    {"paper_id": f"p{n}", "title": "A study of things " * 4}
                    for n in range(375)
                ],
            },
        }
        before = len(json.dumps(detail))

        after = len(json.dumps(_compact_stage_detail(detail)))

        assert before > 3_000_000, before
        assert after < before / 20, (before, after)


class TestStageWallTimeIntegrity:
    """#47 -- a stage that consumed tokens must never report a silent zero."""

    def test_measured_stage_reports_a_real_duration(self, tmp_path: Path):
        observer = HarnessObservability(tmp_path, "run-wall-measured")
        observer.start_run(entry_mode="query_plan")

        observer.start_stage("query_planner")
        duration = observer.finish_stage(
            "query_planner",
            "completed",
            estimated_cost_cny=0.12,
            input_tokens=120,
            output_tokens=30,
        )

        assert duration >= 0.0
        finished = [
            event
            for event in _events(tmp_path)
            if event.get("event") == "stage_finished"
        ]
        assert finished[-1]["wall_time_measured"] is True

    def test_adapter_supplied_wall_time_is_accepted(self, tmp_path: Path):
        """Stages that run inside an adapter measure themselves and pass it in.

        ``publication_mainline_commander`` and
        ``publication_mainline_staged_completion`` execute past the reach of the
        orchestrator's timer, so the duration arrives out-of-band.
        """

        observer = HarnessObservability(tmp_path, "run-wall-adapter")
        observer.start_run(entry_mode="query_plan")

        duration = observer.finish_stage(
            "publication_mainline_commander",
            "completed",
            cost_cny=0.600954,
            input_tokens=955_104,
            output_tokens=12_000,
            wall_time_seconds=734.5,
        )

        assert duration == 734.5
        event = _events(tmp_path)[-1]
        assert event["wall_time_seconds"] == 734.5
        assert event["wall_time_measured"] is True

    def test_untimed_stage_is_flagged_rather_than_claiming_zero_seconds(
        self, tmp_path: Path
    ):
        """The exact rhr_be780761 shape: billed tokens, no timer, no duration.

        A zero here is a *missing measurement*, not a fast stage, and the event
        has to say which one it is.
        """

        observer = HarnessObservability(tmp_path, "run-wall-untimed")
        observer.start_run(entry_mode="query_plan")

        duration = observer.finish_stage(
            "publication_mainline_staged_completion",
            "completed",
            cost_cny=0.661369,
            input_tokens=525_857,
            output_tokens=48_000,
        )

        assert duration == 0.0
        event = _events(tmp_path)[-1]
        assert event["wall_time_seconds"] == 0.0
        assert event["wall_time_measured"] is False

    def test_a_zero_duration_never_looks_measured_when_tokens_were_spent(
        self, tmp_path: Path
    ):
        """The invariant the post-mortem asked for, stated directly.

        No stage may report a measured wall time of zero while also reporting
        token consumption -- that combination is what hid two paid stages.
        """

        observer = HarnessObservability(tmp_path, "run-wall-invariant")
        observer.start_run(entry_mode="query_plan")
        observer.finish_stage(
            "publication_mainline_commander",
            "completed",
            cost_cny=0.600954,
            input_tokens=955_104,
            output_tokens=12_000,
        )
        observer.finish_stage(
            "publication_mainline_handoff",
            "completed",
            wall_time_seconds=1.5,
            input_tokens=0,
            output_tokens=0,
        )

        for event in _events(tmp_path):
            if event.get("event") != "stage_finished":
                continue
            spent = int(event.get("input_tokens", 0)) + int(
                event.get("output_tokens", 0)
            )
            if spent and event.get("wall_time_measured"):
                assert event["wall_time_seconds"] > 0.0, event["stage"]

    def test_negative_supplied_wall_time_is_clamped(self, tmp_path: Path):
        observer = HarnessObservability(tmp_path, "run-wall-clamp")
        observer.start_run(entry_mode="query_plan")

        duration = observer.finish_stage(
            "publication_mainline_handoff",
            "completed",
            wall_time_seconds=-5.0,
        )

        assert duration == 0.0

    def test_start_stage_wins_over_a_supplied_duration(self, tmp_path: Path):
        """A real timer is more trustworthy than a caller's arithmetic."""

        observer = HarnessObservability(tmp_path, "run-wall-precedence")
        observer.start_run(entry_mode="query_plan")

        observer.start_stage("article_structure_audit")
        duration = observer.finish_stage(
            "article_structure_audit",
            "completed",
            wall_time_seconds=9_999.0,
        )

        assert duration < 9_999.0
        assert _events(tmp_path)[-1]["wall_time_measured"] is True
