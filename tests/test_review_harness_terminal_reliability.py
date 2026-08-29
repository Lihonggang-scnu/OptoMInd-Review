from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from optomind_research.runtime.review_harness_orchestrator import (
    ReviewHarnessConfig,
    ReviewHarnessOrchestrator,
    _delivery_quality_report,
    _quality_report_hard_blocks,
)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _events(run_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (run_dir / "HARNESS_EVENTS.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]


def _config(tmp_path: Path) -> ReviewHarnessConfig:
    return ReviewHarnessConfig(
        query_plan_path=tmp_path / "query.json",
        base_kb_sqlite=tmp_path / "kb.sqlite",
        output_root=tmp_path,
    )


def test_live_phase3_defaults_enable_central_claim_pool_profile(tmp_path: Path) -> None:
    query = tmp_path / "query.json"
    query.write_text("{}", encoding="utf-8")
    base = tmp_path / "kb.sqlite"
    base.write_bytes(b"")
    config = ReviewHarnessConfig(
        query_plan_path=query,
        base_kb_sqlite=base,
        output_root=tmp_path / "outputs",
        visual_test_mode=False,
    )
    orchestrator = ReviewHarnessOrchestrator(config, run_dir=tmp_path / "run")
    options = orchestrator._phase3_runtime_options(section_count=7)
    assert options["real_llm_claims"] is True
    assert options["claim_pool_enabled"] is True
    assert options["claim_pool_served_limit"] == 200
    assert options["claim_pool_target_range"] == [100, 140]
    assert options["claim_pool_shortlist_limit"] == 32
    assert options["authoring_core_chunk_limit"] == 12


def _valid_query_plan() -> dict:
    return {
        "input": {
            "user_query": (
                "Achromatic metalenses for augmented reality near-eye displays"
            )
        },
        "output": {
            "problem_understanding": (
                "Review achromatic metalenses for augmented reality near-eye "
                "displays across bandwidth, efficiency, field of view, "
                "fabrication tolerance, and large-area integration."
            ),
            "scope_definition": {
                "main_scope": (
                    "Achromatic metalens architectures and near-eye display "
                    "integration."
                ),
                "scope_items": [
                    "Dispersion engineering",
                    "Broadband metasurface lenses",
                    "Near-eye display constraints",
                ],
            },
            "keyword_decomposition": {
                "keywords": [
                    "achromatic metalens augmented reality",
                    "broadband achromatic metalens",
                    "near-eye display metasurface lens",
                    "metalens field of view",
                    "large area metalens fabrication",
                ]
            },
            "extra_notes": "",
        },
    }


def test_quality_fail_open_preserves_advice_and_blocks_identity_errors() -> None:
    advisory = {
        "status": "needs_attention",
        "blocking_issues": [],
        "warnings": ["low_review_wide_source_diversity"],
    }
    assert _quality_report_hard_blocks(advisory) is False
    delivery = _delivery_quality_report(advisory)
    assert delivery["status"] == "passed"
    assert delivery["original_status"] == "needs_attention"
    assert delivery["warnings"] == advisory["warnings"]
    assert delivery["delivery_fail_open"] is True

    for blocker in (
        "review_topic_identity_mismatch",
        "visual_plan_topic_identity_mismatch",
        "research_plan_topic_identity_mismatch",
    ):
        report = {"status": "needs_attention", "blocking_issues": [blocker]}
        assert _quality_report_hard_blocks(report) is True
        assert _delivery_quality_report(report) == report


def _assert_terminal_consistency(
    run_dir: Path,
    *,
    expect_terminal_error: str | None = None,
) -> None:
    state = _read_json(run_dir / "HARNESS_STATE.json")
    metrics = _read_json(run_dir / "HARNESS_METRICS.json")
    cost = _read_json(run_dir / "HARNESS_COST.json")
    finished = [
        event for event in _events(run_dir) if event.get("event") == "run_finished"
    ]

    assert finished, "terminal run_finished event must be durable"
    terminal = finished[-1]
    assert state["status"] != "running"
    assert metrics["status"] == state["status"] == cost["status"]
    assert metrics["current_stage"] == state["current_stage"] == cost[
        "current_stage"
    ]
    assert terminal["status"] == state["status"]
    assert terminal["current_stage"] == state["current_stage"]
    assert terminal["error_count"] == state["error_count"]
    assert terminal["error_count"] == metrics["operations"]["error_count"]
    assert terminal["reconciliation_id"] == state[
        "terminal_reconciliation_id"
    ]
    assert terminal["reconciliation_id"] == metrics[
        "terminal_reconciliation_id"
    ]
    assert terminal["reconciliation_id"] == cost[
        "terminal_reconciliation_id"
    ]
    # 业务性 fail-closed（稿子空、产物缺、门不通过）与收尾路径的程序崩溃
    # 是两回事：前者 terminal_error 必须为 None，后者必须有 type。
    # 默认断言「干净终态」，让「崩溃冒充失败」在写入的那一刻就被拦住。
    actual = (state.get("terminal_error") or {}).get("type")
    if expect_terminal_error is None:
        assert actual is None, (
            "终态不该带 terminal_error：说明收尾路径抛了未预期异常，"
            f"被兜底伪装成了 fail-closed（实际 type={actual}）"
        )
    else:
        assert actual == expect_terminal_error


def test_cold_import_defines_structured_module_logger(tmp_path: Path) -> None:
    script = (
        "from optomind_research.runtime import review_harness_orchestrator as m; "
        "assert m.logger.name == m.__name__; "
        "assert callable(m.logger.info)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_preflight_is_read_only_for_existing_run_state(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    run_dir = tmp_path / "existing-run"
    query_dir = run_dir / "query_planner"
    query_dir.mkdir(parents=True)
    question = "How do optical metasurfaces encode phase?"
    question_path = tmp_path / "question.txt"
    question_path.write_text(question, encoding="utf-8")
    (query_dir / "ORIGINAL_USER_QUESTION.json").write_text(
        json.dumps({"user_question": question}), encoding="utf-8"
    )
    (query_dir / "query_plan.json").write_text("{}", encoding="utf-8")
    (run_dir / "QUERY_PLAN_ENTRY_GATE.json").write_text(
        json.dumps({"status": "passed", "execution_ready": True}),
        encoding="utf-8",
    )
    state_text = json.dumps({"status": "running", "current_stage": "authoring"})
    event_text = json.dumps({"event": "sentinel"}) + "\n"
    (run_dir / "HARNESS_STATE.json").write_text(state_text, encoding="utf-8")
    (run_dir / "HARNESS_EVENTS.jsonl").write_text(event_text, encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "run_review_harness.py",
            "--question-file",
            str(question_path),
            "--run-dir",
            str(run_dir),
            "--preflight-only",
        ],
        cwd=project_root,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert (run_dir / "HARNESS_STATE.json").read_text(encoding="utf-8") == state_text
    assert (run_dir / "HARNESS_EVENTS.jsonl").read_text(encoding="utf-8") == event_text
    report = _read_json(run_dir / "COST_PREFLIGHT.json")
    assert report["stage_hard_caps_cny"] == {}
    assert report["upstream_query_planner_allowance_cny"] == 0.0
    assert report["observed_spend_cny"]["query_planner"] == 0.0


def test_query_planner_confirmation_stop_has_one_terminal_contract(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from optomind_research.query_planner import QueryPlannerAgent
    from run_review_harness import main

    run_dir = tmp_path / "confirmation-stop"
    package = {
        "status": "primary_valid",
        "needs_human_confirmation": True,
        "result": _valid_query_plan(),
        "final_validation": {"ok": True, "errors": [], "warnings": []},
    }
    monkeypatch.setattr(
        QueryPlannerAgent,
        "plan_review_dict",
        lambda self, question: package,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_review_harness.py",
            "--question",
            "Review achromatic metalenses for near-eye displays.",
            "--run-dir",
            str(run_dir),
        ],
    )

    assert main() == 3
    stdout = json.loads(capsys.readouterr().out)
    assert stdout["status"] == "awaiting_query_plan_confirmation"
    assert stdout["planner_generation_status"] == "primary_valid"
    assert stdout["execution_ready"] is True
    _assert_terminal_consistency(run_dir)

    state = _read_json(run_dir / "HARNESS_STATE.json")
    package_receipt = _read_json(run_dir / "REVIEW_CONTENT_PACKAGE.json")
    assert state["stages"]["query_planner"]["status"] == (
        "awaiting_human_confirmation"
    )
    assert package_receipt["status"] == stdout["status"]
    assert package_receipt["terminal_reconciliation_id"] == state[
        "terminal_reconciliation_id"
    ]


def test_query_planner_recovery_stop_has_one_terminal_contract(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from optomind_research.query_planner import QueryPlannerAgent
    from run_review_harness import main

    run_dir = tmp_path / "model-recovery-stop"
    fallback = {
        "input": {"user_query": "General optical literature scope"},
        "output": {
            "problem_understanding": (
                "Reformulate the research question into a scholarly target."
            ),
            "scope_definition": {
                "main_scope": "General optical literature scope.",
                "scope_items": ["General background"],
            },
            "keyword_decomposition": {
                "keywords": ["optical thin film", "multilayer coating"]
            },
            "extra_notes": "",
        },
    }
    package = {
        "status": "deterministic_fallback_after_repair_failed",
        "needs_human_confirmation": True,
        "result": fallback,
        "final_validation": {"ok": True, "errors": [], "warnings": []},
    }
    monkeypatch.setattr(
        QueryPlannerAgent,
        "plan_review_dict",
        lambda self, question: package,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_review_harness.py",
            "--question",
            "Review a new optical research question.",
            "--auto-confirm-query-plan",
            "--run-dir",
            str(run_dir),
        ],
    )

    assert main() == 3
    stdout = json.loads(capsys.readouterr().out)
    assert stdout["status"] == "needs_model_recovery"
    assert stdout["planner_generation_status"] == (
        "deterministic_fallback_after_repair_failed"
    )
    assert stdout["execution_ready"] is False
    _assert_terminal_consistency(run_dir)

    state = _read_json(run_dir / "HARNESS_STATE.json")
    package_receipt = _read_json(run_dir / "REVIEW_CONTENT_PACKAGE.json")
    assert state["stages"]["query_planner"]["status"] == "failed_closed"
    assert package_receipt["status"] == stdout["status"]
    assert not (run_dir / "review_lead").exists()


def test_unexpected_exception_is_fail_closed_and_resumable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "exception-run"
    harness = ReviewHarnessOrchestrator(
        _config(tmp_path),
        run_dir=run_dir,
    )

    def raise_unexpected() -> object:
        raise RuntimeError("synthetic orchestrator failure")

    monkeypatch.setattr(harness, "_run_impl", raise_unexpected)
    result = harness.run()

    assert result.status == "failed"
    assert result.completed_stage == "orchestrator"
    # 该测试故意注入 RuntimeError：终态必须显式携带该类型的 terminal_error。
    _assert_terminal_consistency(run_dir, expect_terminal_error="RuntimeError")
    events = _events(run_dir)
    assert any(event.get("event") == "run_error" for event in events)
    assert any(
        event.get("event") == "error"
        and event.get("error_type") == "RuntimeError"
        for event in events
    )


def test_empty_review_body_cannot_be_packaged_as_complete(
    tmp_path: Path,
) -> None:
    query_plan = tmp_path / "query.json"
    query_plan.write_text("{}", encoding="utf-8")
    (tmp_path / "kb.sqlite").write_bytes(b"")
    run_dir = tmp_path / "empty-draft-run"
    empty_review = run_dir / "authoring" / "FINAL_REVIEW_EN.md"
    empty_review.parent.mkdir(parents=True)
    empty_review.write_text(" \n\t\n", encoding="utf-8")
    harness = ReviewHarnessOrchestrator(
        _config(tmp_path),
        run_dir=run_dir,
    )

    result = harness._finish(
        "completed",
        "packaging",
        empty_review,
        None,
    )
    package = _read_json(result.package_path)

    assert result.status == "failed"
    assert package["status"] == "failed"
    assert package["final_review_path"] == ""
    assert package["review_body_validation"]["reason"] == "empty_review_body"
    assert package["quality_gate"]["status"] == "failed"
    _assert_terminal_consistency(run_dir)


def test_business_fail_closed_carries_no_terminal_error(tmp_path: Path) -> None:
    """正面固定「合法失败不带 terminal_error」这半边语义。

    业务性 fail-closed（空稿被正确拒绝）与收尾路径的程序崩溃必须有相反的
    terminal_error 签名；默认断言负责拒绝后者，本测试负责钉住前者。
    """

    query_plan = tmp_path / "query.json"
    query_plan.write_text("{}", encoding="utf-8")
    (tmp_path / "kb.sqlite").write_bytes(b"")
    run_dir = tmp_path / "business-fail-closed-run"
    empty_review = run_dir / "authoring" / "FINAL_REVIEW_EN.md"
    empty_review.parent.mkdir(parents=True)
    empty_review.write_text(" \n\t\n", encoding="utf-8")
    harness = ReviewHarnessOrchestrator(
        _config(tmp_path),
        run_dir=run_dir,
    )

    result = harness._finish("completed", "packaging", empty_review, None)

    assert result.status == "failed"
    state = _read_json(run_dir / "HARNESS_STATE.json")
    assert (state.get("terminal_error") or {}).get("type") is None
    _assert_terminal_consistency(run_dir)


def test_finalization_breadcrumb_failure_still_recovers_terminal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """留痕自身失败，也不许挡住四方一致的终态收尾。

    ``observability.fail`` 走 append_jsonl 做真实磁盘 I/O 且本身不设兜底：
    磁盘满 / 权限 / 文件被占用都会抛。若该异常穿过 ``_finish`` 的兜底，
    ``_recover_terminal`` 就永不执行、状态停在 running、run 不可恢复——
    正是 T-01 禁止的回归。本测试钉住「留痕可以失败，收尾不许失败」。
    """

    query_plan = tmp_path / "query.json"
    query_plan.write_text("{}", encoding="utf-8")
    (tmp_path / "kb.sqlite").write_bytes(b"")
    run_dir = tmp_path / "breadcrumb-io-failure-run"
    harness = ReviewHarnessOrchestrator(
        _config(tmp_path),
        run_dir=run_dir,
    )

    def explode(**_kwargs: object) -> None:
        raise OSError("synthetic events.jsonl write failure")

    def raise_in_finish_impl(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("synthetic finalization defect")

    monkeypatch.setattr(harness.observability, "fail", explode)
    monkeypatch.setattr(harness, "_finish_impl", raise_in_finish_impl)

    result = harness._finish("completed", "packaging", None, None)

    # 收尾照样完成：仍 fail closed，状态不停在 running。
    assert result.status == "failed"
    state = _read_json(run_dir / "HARNESS_STATE.json")
    assert state.get("status") == "failed"
    # 缺陷签名仍然写进了 terminal_error——留痕丢的只是事件流那一份。
    _assert_terminal_consistency(run_dir, expect_terminal_error="RuntimeError")


def test_missing_review_body_cannot_be_reported_complete(tmp_path: Path) -> None:
    query_plan = tmp_path / "query.json"
    query_plan.write_text("{}", encoding="utf-8")
    (tmp_path / "kb.sqlite").write_bytes(b"")
    run_dir = tmp_path / "missing-draft-run"
    harness = ReviewHarnessOrchestrator(
        _config(tmp_path),
        run_dir=run_dir,
    )

    result = harness._finish("completed", "packaging", None, None)
    package = _read_json(result.package_path)

    assert result.status != "completed"
    assert package["final_review_path"] == ""
    assert package["review_body_validation"]["reason"] == "missing_review_body"
    _assert_terminal_consistency(run_dir)


def test_normal_failed_terminal_path_reconciles_all_artifacts(
    tmp_path: Path,
) -> None:
    harness = ReviewHarnessOrchestrator(
        _config(tmp_path),
        run_dir=tmp_path / "normal-failure-run",
    )

    result = harness.run()

    assert result.status == "failed"
    assert result.completed_stage == "query_plan_missing"
    _assert_terminal_consistency(result.work_dir)


def test_startup_repairs_state_when_terminal_event_already_exists(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "stale-terminal-run"
    run_dir.mkdir()
    (run_dir / "HARNESS_STATE.json").write_text(
        json.dumps(
            {
                "schema_version": "research_harness.state.v1",
                "run_id": "stale-terminal-run",
                "status": "running",
                "current_stage": "section_coverage",
                "stages": {},
                "created_at": "2026-08-03T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "HARNESS_EVENTS.jsonl").write_text(
        json.dumps(
            {
                "event": "run_finished",
                "status": "failed",
                "current_stage": "section_coverage",
                "error_count": 1,
                "reconciliation_id": "term_existing",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    harness = ReviewHarnessOrchestrator(
        _config(tmp_path),
        run_dir=run_dir,
    )

    state = _read_json(run_dir / "HARNESS_STATE.json")
    cost = _read_json(run_dir / "HARNESS_COST.json")
    assert state["status"] == "failed"
    assert state["current_stage"] == "section_coverage"
    assert state["error_count"] == 1
    assert cost["status"] == "failed"
    assert cost["current_stage"] == "section_coverage"
    assert cost["terminal_reconciliation_id"] == "term_existing"
    assert harness.state["status"] == "failed"


def _harness_cli(
    *args: str,
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    project_root = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [sys.executable, "run_review_harness.py", *args],
        cwd=project_root,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_second_harness_process_fails_before_writing_run_artifacts(
    tmp_path: Path,
) -> None:
    from run_review_harness import RUN_DIR_LOCK_FILENAME

    project_root = Path(__file__).resolve().parents[1]
    run_dir = tmp_path / "locked-run"
    run_dir.mkdir()
    ready = tmp_path / "lock-ready"
    release = tmp_path / "lock-release"
    sentinel = run_dir / "SENTINEL.json"
    sentinel.write_text('{"untouched": true}', encoding="utf-8")

    holder_script = (
        "import json\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "from run_review_harness import RunDirectoryLock\n"
        "lock = RunDirectoryLock(Path(sys.argv[1]))\n"
        "lock.acquire()\n"
        "Path(sys.argv[2]).write_text("
        "json.dumps({'pid': os.getpid()}), encoding='utf-8')\n"
        "while not Path(sys.argv[3]).exists():\n"
        "    time.sleep(0.05)\n"
        "lock.release()\n"
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            holder_script,
            str(run_dir),
            str(ready),
            str(release),
        ],
        cwd=project_root,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if holder.poll() is not None:
                _out, err = holder.communicate()
                raise AssertionError(f"lock holder exited early: {err}")
            if time.monotonic() > deadline:
                raise AssertionError("lock holder did not acquire in time")
            time.sleep(0.05)
        owner_pid = json.loads(ready.read_text(encoding="utf-8"))["pid"]
        question_path = tmp_path / "question.txt"
        question_path.write_text(
            "How do optical metasurfaces encode phase?",
            encoding="utf-8",
        )
        contested = _harness_cli(
            "--question-file",
            str(question_path),
            "--run-dir",
            str(run_dir),
            "--preflight-only",
        )
        assert contested.returncode == 1
        assert str(owner_pid) in contested.stderr
        assert not (run_dir / "COST_PREFLIGHT.json").exists()
        assert not (run_dir / "HARNESS_EVENTS.jsonl").exists()
        assert not (run_dir / "HARNESS_STATE.json").exists()
        assert (run_dir / RUN_DIR_LOCK_FILENAME).exists()
        assert sentinel.read_text(encoding="utf-8") == '{"untouched": true}'
    finally:
        release.write_text("go", encoding="utf-8")
        try:
            holder.wait(timeout=15)
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=5)
    assert not (run_dir / RUN_DIR_LOCK_FILENAME).exists()


def test_stale_run_lock_is_recovered_before_writing_artifacts(
    tmp_path: Path,
) -> None:
    from run_review_harness import (
        RUN_DIR_LOCK_FILENAME,
        _pid_is_alive,
    )

    project_root = Path(__file__).resolve().parents[1]
    run_dir = tmp_path / "stale-lock-run"
    run_dir.mkdir()
    dead_pid = None
    for _ in range(5):
        probe = subprocess.run(
            [sys.executable, "-c", "import os; print(os.getpid())"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        candidate = int(probe.stdout.strip())
        if not _pid_is_alive(candidate):
            dead_pid = candidate
            break
    assert dead_pid is not None

    lock_path = run_dir / RUN_DIR_LOCK_FILENAME
    lock_path.write_text(
        json.dumps(
            {
                "schema_version": "optomind.run_dir_lock.v1",
                "pid": dead_pid,
                "token": "stale-token",
                "run_dir": str(run_dir),
                "hostname": "stale-host",
                "acquired_at": "2020-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    question_path = tmp_path / "question.txt"
    question_path.write_text(
        "How do optical metasurfaces encode phase?",
        encoding="utf-8",
    )
    completed = _harness_cli(
        "--question-file",
        str(question_path),
        "--run-dir",
        str(run_dir),
        "--preflight-only",
    )
    assert completed.returncode == 0, completed.stderr
    report = _read_json(run_dir / "COST_PREFLIGHT.json")
    assert report["stage_hard_caps_cny"] == {}
    assert report["upstream_query_planner_allowance_cny"] == 1.0
    assert not lock_path.exists()


def test_run_directory_lock_reports_live_owner_and_releases(
    tmp_path: Path,
) -> None:
    from run_review_harness import (
        RunDirectoryLock,
        RunDirectoryLockError,
    )

    run_dir = tmp_path / "lock-unit"
    run_dir.mkdir()
    first = RunDirectoryLock(run_dir)
    first.acquire()
    payload = _read_json(first.lock_path)
    assert payload["pid"] == os.getpid()

    second = RunDirectoryLock(run_dir)
    with pytest.raises(RunDirectoryLockError) as exc_info:
        second.acquire()
    assert exc_info.value.owner_pid == os.getpid()
    assert str(os.getpid()) in str(exc_info.value)

    first.release()
    assert not first.lock_path.exists()


def test_run_directory_lock_releases_on_exception_and_cleans_only_own_lock(
    tmp_path: Path,
) -> None:
    from run_review_harness import RunDirectoryLock

    run_dir = tmp_path / "release-unit"
    run_dir.mkdir()
    lock = RunDirectoryLock(run_dir)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        try:
            lock.acquire()
            raise RuntimeError("synthetic failure")
        finally:
            lock.release()
    assert not lock.lock_path.exists()

    replaced = RunDirectoryLock(run_dir)
    replaced.acquire()
    replaced.lock_path.write_text(
        json.dumps(
            {
                "schema_version": "optomind.run_dir_lock.v1",
                "pid": os.getpid(),
                "token": "different-owner",
                "run_dir": str(run_dir),
                "hostname": "other-host",
                "acquired_at": "2020-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    replaced.release()
    assert replaced.lock_path.exists()


def test_failed_harness_invocation_releases_run_lock(
    tmp_path: Path,
) -> None:
    from run_review_harness import RUN_DIR_LOCK_FILENAME

    run_dir = tmp_path / "exception-release-run"
    completed = _harness_cli(
        "--query-plan",
        str(tmp_path / "missing-plan.json"),
        "--run-dir",
        str(run_dir),
    )
    assert completed.returncode != 0
    assert (run_dir / "HARNESS_EVENTS.jsonl").exists()
    assert not (run_dir / RUN_DIR_LOCK_FILENAME).exists()


def test_review_harness_config_translation_fail_open_default() -> None:
    """P3-4: config default must match the CLI default (fail-open)."""
    config = ReviewHarnessConfig(
        query_plan_path=Path("qp.json"),
        base_kb_sqlite=Path("kb.sqlite"),
        output_root=Path("out"),
    )
    assert config.translation_fail_open is True
