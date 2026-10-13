"""Offline seam checks for the downstream supervision launcher.

The tests use only temporary SQLite ledgers and an in-process fake provider.
They never resolve a key, open a socket, or touch the locked v19 outputs.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
SUPERVISOR_ROOT = HERE.parent / "supervisor"
if str(SUPERVISOR_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPERVISOR_ROOT))

import downstream_supervision as launcher
from supervised_client import decide
from optomind_research.runtime.upgrade3.module4 import runtime
from optomind_research.runtime.upgrade3.dual_budget import DualBudgetLedger


SOURCE = launcher.SOURCE_ROOT
OriginalProvider = runtime.QwenDirectClient


class OfflineProvider(OriginalProvider):
    """Native constructor shape with a call that only settles temp ledgers."""

    calls: list[dict] = []
    amount = 0.01
    cost = 0.003

    def _keys(self):  # pragma: no cover - a guard if the seam regresses.
        raise AssertionError("offline_test_must_not_resolve_credentials")

    def __call__(self, messages, **kwargs):
        call_id = str(kwargs.get("call_id") or "offline")
        row = self.budget_ledger.reserve(self.amount, call_id + ":key0:attempt0")
        self.budget_ledger.settle(row["reservation_id"], self.cost, uncertain=False,
                                  telemetry={"offline_fake": True})
        self.calls.append({"messages": deepcopy(messages), "kwargs": deepcopy(kwargs),
                           "provider": self})
        if call_id.startswith("chapter-arrangement:"):
            payload = json.loads(messages[-1]["content"])
            tasks = payload["units"][0]["existing_paragraph_tasks"]
            value = {
                "chapter_id": "CH01",
                "units": [{"unit_id": "U1", "paragraph_tasks": [
                    {"source_briefs": [item["paragraph_id"]]} for item in tasks
                ]}],
            }
        else:
            value = {"status": "fulfilled", "useful_material": "offline synthetic"}
        return {
            "content": json.dumps(value),
            "complete": True,
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "offline_fake": True,
        }


@pytest.fixture(autouse=True)
def reset_fake_calls():
    OfflineProvider.calls = []


@pytest.fixture
def env(tmp_path):
    project = tmp_path / "project.sqlite"
    account = tmp_path / "account.sqlite"
    runtime.GlobalBudgetLedger(path=project, limit_cny=80.0)
    runtime.GlobalBudgetLedger(path=account, limit_cny=300.0)
    value = json.loads(launcher.DEFAULT_BINDING.read_text(encoding="utf-8"))
    key_file = tmp_path / "NEVER_READ.key"
    key_file.write_text("offline-placeholder\n", encoding="utf-8")
    value.update(
        project_ledger=str(project),
        account_ledger=str(account),
        source_worktree=str(SOURCE),
        key_file=str(key_file),
    )
    binding = tmp_path / "BINDING.json"
    binding.write_text(json.dumps(value), encoding="utf-8")
    return {
        "root": tmp_path,
        "binding": binding,
        "project": project,
        "account": account,
        "key": key_file,
        "supervision": tmp_path / "supervision",
    }


def rows(path: Path):
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        return db.execute(
            "SELECT call_id, amount_cny, actual_cny, status FROM reservations"
        ).fetchall()


def make_packet(root: Path) -> Path:
    packet_root = root / "packet"
    (packet_root / "writer_packets").mkdir(parents=True)
    packet = {
        "chapter_id": "CH01",
        "chapter": {"chapter_id": "CH01", "title": "Synthetic"},
        "chapter_plan": {"units": [{
            "unit_id": "U1",
            "substantive_point": "Synthetic point",
            "paragraph_briefs": [{
                "paragraph_id": "B1",
                "point": "Synthetic point",
                "development": "Synthetic task",
                "source_handles": ["P0001"],
            }],
        }]},
        "source_materials": [{
            "source_handle": "P0001",
            "paper_id": "synthetic",
            "study_summary_A": {"finding": "Synthetic material"},
        }],
    }
    (packet_root / "writer_packets" / "CH01.json").write_text(
        json.dumps(packet), encoding="utf-8"
    )
    (packet_root / "DETAILED_REVIEW_PLAN.json").write_text(
        json.dumps({"shared_outline": [{"chapter_id": "CH01"}]}), encoding="utf-8"
    )
    pointer = root / "CURRENT_PLAN.json"
    pointer.write_text(json.dumps({"packet_root": str(packet_root)}), encoding="utf-8")
    return pointer


@contextmanager
def approve_all(root: Path):
    """Approve only synthetic requests/responses in this temp supervision root."""

    stop = threading.Event()
    errors: list[BaseException] = []

    def review():
        try:
            while not stop.is_set():
                for request_path in root.glob("calls/*/REQUEST.json"):
                    directory = request_path.parent
                    request = json.loads(request_path.read_text(encoding="utf-8"))
                    if not (directory / "REQUEST_DECISION.json").exists():
                        decide(root=root, stage=request["stage"], call_id=request["call_id"],
                               phase="request", decision="approve", reason="offline test")
                    if (directory / "RESPONSE.json").exists() and not (directory / "RESPONSE_DECISION.json").exists():
                        decide(root=root, stage=request["stage"], call_id=request["call_id"],
                               phase="response", decision="approve", reason="offline test")
                time.sleep(0.005)
        except BaseException as exc:  # propagate through the context manager
            errors.append(exc)

    worker = threading.Thread(target=review, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=3)
        assert not errors, errors


def arrangement_argv(env, *, run: bool) -> list[str]:
    pointer = make_packet(env["root"])
    args = [
        "--packet-root", str(pointer),
        "--output-root", str(env["root"] / ("arrangement_run" if run else "arrangement_preview")),
        "--all-chapters", "--planning-revision",
        "--key-file", str(env["key"]),
        "--budget-ledger", str(env["project"]),
        "--round-cap-cny", "10", "--no-retry",
    ]
    if run:
        args.append("--run")
    return args


def test_default_preview_dispatches_formal_cli_without_provider(env, monkeypatch):
    from scripts.upgrade3 import chapter_arrangement
    monkeypatch.setattr(chapter_arrangement, "load_editor_prompt", lambda **kwargs: "Offline editor prompt")
    before_allowlist = launcher._planning_transport._PLANNING_MODULES
    result = launcher.run_formal(
        "arrangement", arrangement_argv(env, run=False),
        binding_path=env["binding"], supervision_root=env["supervision"],
        fake_provider=OfflineProvider, pin_check=lambda binding: None,
    )
    assert result.returncode == 0
    assert not result.run_enabled
    assert not OfflineProvider.calls
    assert not list(env["supervision"].glob("calls/*"))
    assert not rows(env["project"]) and not rows(env["account"])
    assert launcher._planning_transport._PLANNING_MODULES == before_allowlist


def test_explicit_run_reuses_dual_gates_for_arrangement_and_downstream_factory_profiles(env, monkeypatch):
    from scripts.upgrade3 import chapter_arrangement
    monkeypatch.setattr(chapter_arrangement, "load_editor_prompt", lambda **kwargs: "Offline editor prompt")
    before_allowlist = launcher._planning_transport._PLANNING_MODULES
    with approve_all(env["supervision"]):
        result = launcher.run_formal(
            "arrangement", arrangement_argv(env, run=True),
            binding_path=env["binding"], supervision_root=env["supervision"],
            fake_provider=OfflineProvider, pin_check=lambda binding: None,
        )
    adapter = result.adapter
    assert result.returncode == 0 and result.run_enabled
    assert len(OfflineProvider.calls) == 1

    # This is the exact shared live factory used by BODY/parts/checker.  The
    # adapter temporarily admits its production module and restores the old
    # allowlist after the scoped run.
    from scripts.upgrade3.writer_candidates import make_live_factory

    args = SimpleNamespace(
        run=True, responses=None,
        budget_ledger=env["project"], budget_limit=80.0,
        account_ledger=env["account"], account_budget_limit=300.0,
        account_mapping_dir=env["root"] / "account_mappings",
        key_file=env["key"], key_index=1, allow_max=False,
    )
    profiles = [
        ("body_author", {"model": "qwen3.5-plus", "thinking": True,
                         "thinking_budget": 8192, "max_output_tokens": 32768,
                         "json_mode": False, "timeout_seconds": 900.0,
                         "stream": True, "stream_overall_timeout_seconds": 1800.0}),
        ("body_quality", {"model": "qwen3.5-plus", "thinking": True,
                          "thinking_budget": 16384, "max_output_tokens": 24576,
                          "json_mode": False, "timeout_seconds": 900.0,
                          "stream": True, "stream_overall_timeout_seconds": 1800.0}),
        ("body_editor", {"model": "qwen3.5-plus", "thinking": True,
                         "thinking_budget": 16384, "max_output_tokens": 24576,
                         "json_mode": False, "timeout_seconds": 900.0,
                         "stream": True, "stream_overall_timeout_seconds": 1800.0}),
        ("post_body_parts", {"model": "qwen3.5-plus", "thinking": True,
                             "thinking_budget": 16384, "max_output_tokens": 32768,
                             "json_mode": False, "timeout_seconds": 900.0,
                             "stream": True, "stream_overall_timeout_seconds": 1800.0}),
        ("manuscript_checker", {"model": "qwen3.5-plus", "thinking": True,
                                "thinking_budget": 16384, "max_output_tokens": 32768,
                                "json_mode": False, "timeout_seconds": 900.0,
                                "stream": True, "stream_overall_timeout_seconds": 1800.0}),
    ]
    with approve_all(env["supervision"]):
        with launcher.downstream_allowlist(), adapter.activate():
            factory = make_live_factory(args, token_counter=None, thread_safe=False)
            for role, profile in profiles:
                call = factory(role, env["root"] / role, profile)
                call([{"role": "user", "content": "offline synthetic"}], call_id=role)
    assert launcher._planning_transport._PLANNING_MODULES == before_allowlist
    assert len(OfflineProvider.calls) == 6
    assert len(adapter.constructed) == 6
    assert len(rows(env["project"])) == len(rows(env["account"])) == 6
    gates = list(env["supervision"].glob("calls/*"))
    assert len(gates) == 6
    assert all(all((gate / name).is_file() for name in (
        "REQUEST.json", "REQUEST_DECISION.json", "INVOCATION.json",
        "RESPONSE.json", "RESPONSE_DECISION.json")) for gate in gates)
    assert all(
        provider.max_retries == 0 and provider.max_keys == 1 and provider.key_index == 1
        and isinstance(provider.budget_ledger, DualBudgetLedger)
        and provider.budget_ledger.project.path.resolve() == env["project"].resolve()
        and provider.budget_ledger.total.path.resolve() == env["account"].resolve()
        for record in OfflineProvider.calls
        for provider in [record["provider"]]
    )
    profiles_on_disk = [
        json.loads(path.read_text(encoding="utf-8"))["profile"]
        for path in env["supervision"].glob("calls/*/REQUEST.json")
    ]
    assert {profile["module"] for profile in profiles_on_disk} == {
        "scripts.upgrade3.chapter_arrangement", "scripts.upgrade3.writer_candidates"
    }
    assert sum(profile["module"] == "scripts.upgrade3.chapter_arrangement" for profile in profiles_on_disk) == 1
    assert sum(profile["module"] == "scripts.upgrade3.writer_candidates" for profile in profiles_on_disk) == 5
    assert all(profile["model"] == "qwen3.5-plus" for profile in profiles_on_disk)
    assert all(profile["max_retries"] == 0 and profile["max_keys"] == 1 and profile["key_index"] == 1 for profile in profiles_on_disk)
    assert sorted((profile["thinking_budget"], profile["max_output_tokens"]) for profile in profiles_on_disk) == [
        (8192, 32768), (16384, 24576), (16384, 24576),
        (16384, 32768), (16384, 32768), (16384, 32768),
    ]
