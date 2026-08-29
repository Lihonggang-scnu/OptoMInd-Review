"""P2-1 regression tests: human-in-the-loop decision gate.

Covers registration/dedup, append-only ledger bytes, human resolution,
double-resolve rejection, option validation, zero-second auto-accept
(no sleeping), mandatory-human refusal of timeouts, the pytest hard
guard for visual_review, and that delivery_gate never auto-accepts
even under pytest.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from optomind_research.runtime import human_decision_gate as gate


def _read_ledger(run_dir: Path) -> list[dict]:
    path = run_dir / gate.LEDGER_NAME
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _request(run_dir: Path, **overrides):
    kwargs = dict(
        run_dir=run_dir,
        kind="visual_review",
        subject_id="S01",
        context={"figure_kind": "mechanism_schematic"},
        options=["accept", "reject"],
    )
    kwargs.update(overrides)
    return gate.request_decision(**kwargs)


def test_request_creates_pending_and_ledger_row(tmp_path: Path) -> None:
    decision_id = _request(tmp_path)
    assert len(decision_id) == 12
    assert (tmp_path / "PENDING_DECISIONS" / f"{decision_id}.json").is_file()
    rows = _read_ledger(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    for key in ("ts", "event", "decision_id", "kind", "subject_id", "options"):
        assert key in row
    assert row["event"] == "requested"
    assert row["kind"] == "visual_review"


def test_ledger_is_append_only_across_resolution(tmp_path: Path) -> None:
    decision_id = _request(tmp_path)
    ledger = tmp_path / gate.LEDGER_NAME
    before = ledger.read_bytes()
    gate.resolve_decision(
        tmp_path, decision_id, "accept", actor="human:alice", note="looks fine"
    )
    after = ledger.read_bytes()
    # History is a strict byte-prefix of the present: nothing rewritten.
    assert after.startswith(before)
    assert len(after) > len(before)


def test_same_kind_subject_dedupes_to_one_pending(tmp_path: Path) -> None:
    # delivery_gate never expires (mandatory human), so the pending item
    # survives for the dedup assertion.
    first = _request(
        tmp_path, kind="delivery_gate", subject_id="run-1", context={"a": 1},
    )
    second = _request(
        tmp_path, kind="delivery_gate", subject_id="run-1", context={"a": 2},
    )
    assert first == second
    assert len(gate.list_pending(tmp_path)) == 1
    requested_rows = [
        row for row in _read_ledger(tmp_path) if row["event"] == "requested"
    ]
    assert len(requested_rows) == 1


def test_resolve_records_human_answer(tmp_path: Path) -> None:
    decision_id = _request(tmp_path)
    gate.resolve_decision(
        tmp_path,
        decision_id,
        "accept",
        actor="human:alice",
        note="caption ok",
    )
    state = gate.decision_state(tmp_path, decision_id)
    assert state["state"] == "resolved"
    assert state["chosen"] == "accept"
    assert state["actor"] == "human:alice"
    assert state["auto"] is False
    assert state["note"] == "caption ok"
    assert gate.list_pending(tmp_path) == []


def test_double_resolve_raises_and_ledger_untouched(tmp_path: Path) -> None:
    decision_id = _request(tmp_path)
    gate.resolve_decision(tmp_path, decision_id, "accept", actor="human:bob")
    lines_before = len(_read_ledger(tmp_path))
    with pytest.raises(KeyError):
        gate.resolve_decision(
            tmp_path, decision_id, "reject", actor="human:bob",
        )
    assert len(_read_ledger(tmp_path)) == lines_before


def test_unknown_decision_id_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        gate.decision_state(tmp_path, "deadbeef0000")


def test_auto_accept_after_zero_seconds_without_sleeping(
    tmp_path: Path,
) -> None:
    # P3-3: reads no longer auto-expire; expiry happens explicitly.
    decision_id = _request(
        tmp_path,
        auto_accept_after_seconds=0,
        default_option="accept",
    )
    state = gate.decision_state(tmp_path, decision_id)
    assert state["state"] == "pending"
    gate.expire_due_decisions(tmp_path)
    state = gate.decision_state(tmp_path, decision_id)
    assert state["state"] == "resolved"
    assert state["actor"] == "auto"
    assert state["auto"] is True
    assert state["chosen"] == "accept"
    assert gate.list_pending(tmp_path) == []


def test_request_decision_expires_due_items(tmp_path: Path) -> None:
    """P3-3: registration is the sanctioned side-effect point."""
    first = _request(
        tmp_path,
        subject_id="S-old",
        auto_accept_after_seconds=0,
        default_option="accept",
    )
    second = _request(tmp_path, subject_id="S-new")
    states = {
        gate.decision_state(tmp_path, first)["state"],
        gate.decision_state(tmp_path, second)["state"],
    }
    assert states == {"resolved", "pending"}


@pytest.mark.parametrize("kind", sorted(gate._MANDATORY_HUMAN_KINDS))
def test_mandatory_kinds_refuse_timeouts(tmp_path: Path, kind: str) -> None:
    before = len(_read_ledger(tmp_path))
    with pytest.raises(ValueError):
        _request(
            tmp_path,
            kind=kind,
            subject_id="subject-x",
            options=["accept", "reject"],
            auto_accept_after_seconds=5,
            default_option="accept",
        )
    assert len(_read_ledger(tmp_path)) == before


def test_resolve_rejects_unregistered_option(tmp_path: Path) -> None:
    # delivery_gate stays pending under pytest, so the still-pending
    # assertion below is meaningful.
    decision_id = _request(
        tmp_path,
        kind="delivery_gate",
        subject_id="run-2",
        context={},
        options=["accept", "reject"],
    )
    before = len(_read_ledger(tmp_path))
    with pytest.raises(ValueError):
        gate.resolve_decision(
            tmp_path, decision_id, "maybe", actor="human:carol",
        )
    assert len(_read_ledger(tmp_path)) == before
    assert len(gate.list_pending(tmp_path)) == 1


def test_pytest_visual_review_never_blocks_indefinitely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests::hard_guard")
    decision_id = _request(tmp_path, kind="visual_review")
    # P3-3: reads are pure, so the zero-second guard resolves on the
    # next explicit expiry (harness loop / dedicated endpoint), not on a
    # read side effect.
    assert gate.decision_state(tmp_path, decision_id)["state"] == "pending"
    gate.expire_due_decisions(tmp_path)
    state = gate.decision_state(tmp_path, decision_id)
    assert state["state"] == "resolved"
    assert state["auto"] is True
    assert state["actor"] == "auto"


def test_delivery_gate_under_pytest_still_waits_for_human(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests::mandatory")
    decision_id = _request(
        tmp_path,
        kind="delivery_gate",
        subject_id="run-123",
        context={"awaiting_human_checks": ["research_plan_publication_audit"]},
        options=["accept", "reject"],
    )
    pending = gate.list_pending(tmp_path)
    assert len(pending) == 1
    assert pending[0]["decision_id"] == decision_id
    state = gate.decision_state(tmp_path, decision_id)
    assert state["state"] == "pending"


def test_list_pending_does_not_expire(tmp_path: Path) -> None:
    """P3-3: list_pending is a pure read even with a due item present."""
    decision_id = _request(
        tmp_path,
        auto_accept_after_seconds=0,
        default_option="accept",
    )
    ledger = tmp_path / gate.LEDGER_NAME
    mtime_before = ledger.stat().st_mtime_ns
    pending_file = (
        tmp_path / gate.PENDING_DIR_NAME / f"{decision_id}.json"
    )
    assert pending_file.is_file()
    items = gate.list_pending(tmp_path)
    assert [item["decision_id"] for item in items] == [decision_id]
    assert ledger.stat().st_mtime_ns == mtime_before
    assert pending_file.is_file()

def test_concurrent_register_same_decision(tmp_path: Path) -> None:
    """P3-5: 100 threads racing on one kind+subject dedupe to one row."""
    import threading

    results: list[str] = []
    errors: list[str] = []

    def do_register() -> None:
        try:
            did = gate.request_decision(
                tmp_path,
                "visual_review",
                "s01",
                context={},
                options=["accept", "reject"],
                auto_accept_after_seconds=3600,
            )
            results.append(did)
        except Exception as exc:
            errors.append(str(exc))

    threads = [
        threading.Thread(target=do_register) for _ in range(100)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [], f"unexpected errors: {errors}"
    assert len(set(results)) == 1, "all calls must return same decision_id"
    pending_files = list((tmp_path / "PENDING_DECISIONS").glob("*.json"))
    assert len(pending_files) == 1, "exactly one pending file"
    rows = _read_ledger(tmp_path)
    assert sum(1 for r in rows if r["event"] == "requested") == 1


def test_concurrent_resolve_same_decision(tmp_path: Path) -> None:
    """P3-5: exactly one of 100 concurrent resolves may win."""
    import threading

    did = gate.request_decision(
        tmp_path,
        "visual_review",
        "s02",
        context={},
        options=["accept", "reject"],
        auto_accept_after_seconds=3600,
    )
    success_count = [0]
    error_count = [0]
    count_lock = threading.Lock()

    def do_resolve() -> None:
        try:
            gate.resolve_decision(
                tmp_path, did, "accept", "tester"
            )
            with count_lock:
                success_count[0] += 1
        except (KeyError, FileNotFoundError):
            with count_lock:
                error_count[0] += 1

    threads = [
        threading.Thread(target=do_resolve) for _ in range(100)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert success_count[0] == 1, "exactly one resolve must succeed"
    assert error_count[0] == 99, "remaining 99 must get KeyError/FileNotFoundError"
    rows = _read_ledger(tmp_path)
    assert sum(1 for r in rows if r["event"] == "resolved") == 1