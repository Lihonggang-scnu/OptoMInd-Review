"""WO-02 LOCAL_ONLY: synthetic snapshots/SQLite; only model boundary substituted."""
import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import directed_reading as dr


class ModelBoundary:
    def __init__(self, *contents):
        self.contents = list(contents)
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return {"content": self.contents.pop(0)}


def answer(text="Synthetic material explains the requested mechanism.", remaining=None):
    return {"question_material": [{"question_id": "Q01", "explanation": text,
        "examples": [], "remaining_points": remaining or []}]}


@pytest.fixture
def reading(tmp_path):
    snapshot = tmp_path / "synthetic_snapshot"
    snapshot.mkdir()
    (snapshot / "READING_VIEW.md").write_text("# Synthetic fixture\nA fictional mechanism operates under controlled conditions.")
    store = dr.DirectedReadingStore(tmp_path / "test-only.sqlite", core_cap=1)
    paper = {"canonical_paper_id": "synthetic-paper", "title": "Synthetic fixture", "material_scope": "fulltext",
        "approve_core": True, "nomination_reason": "Answer the synthetic question", "expected_information_gain": "Explain the fictional mechanism",
        "core_justification": "Required for this synthetic test", "knowledge_gap": "Explain synthetic mechanism details", "required_outputs": ["O01"]}
    request = dr.build_directed_request(review_id="synthetic-review", topic="Synthetic topic", chapter={"chapter_id": "C01", "title": "Synthetic chapter"},
        questions=[{"question_id": "Q01", "question": "How does the mechanism operate?", "purpose": "Explain the mechanism", "required_output_ids": ["O01"], "gap_key": "mechanism"}],
        required_outputs=[{"output_id": "O01", "output_type": "explanation", "description": "Describe the mechanism"}])
    store.admit_candidate(request["review_id"], request["topic_binding"], paper)
    return dict(request=request, paper=paper, snapshot_dir=snapshot, output_dir=tmp_path / "reading", store=store)


def test_real_store_new_question_and_outputs_keep_one_paper(reading):
    client = ModelBoundary(answer(), answer(), answer())
    first = dr.run_directed_reading(**reading, client=client)
    reused = dr.run_directed_reading(**reading, client=client)
    assert reused["reused"] and reused["output_dir"] == first["output_dir"]
    changed = json.loads(json.dumps(reading["request"]))
    changed["questions"][0]["question"] = "What changes the mechanism?"
    second = dr.run_directed_reading(**{**reading, "request": changed}, client=client)
    changed["required_outputs"][0]["description"] = "Explain operating conditions"
    third = dr.run_directed_reading(**{**reading, "request": changed}, client=client)
    assert len({r["output"]["task_id"] for r in (first, second, third)}) == 3
    assert len({r["output_dir"] for r in (first, second, third)}) == 3
    assert len(client.calls) == 3
    assert reading["store"].core_count("synthetic-review") == 1


def test_real_reader_empty_requires_explicit_retry_preserves_raw(reading):
    client = ModelBoundary(answer(""), answer())
    old = dr.run_directed_reading(**reading, client=client)
    old_dir = Path(old["output_dir"])
    old_result = (old_dir / "DIRECTED_READING.json").read_bytes()
    old_raw = (old_dir / "RAW_RESPONSE.json").read_bytes()
    assert not old["ready"]
    retained = dr.run_directed_reading(**reading, client=client)
    assert not retained["reused"] and not retained["fulfilled"]
    assert len(client.calls) == 1
    retried = dr.run_directed_reading(**reading, client=client, retry_empty_result=True)
    assert retried["fulfilled"] and len(client.calls) == 2
    assert retried["output_dir"] != old["output_dir"]
    assert (old_dir / "DIRECTED_READING.json").read_bytes() == old_result
    assert (old_dir / "RAW_RESPONSE.json").read_bytes() == old_raw
    reused = dr.run_directed_reading(**reading, client=client)
    assert reused["reused"] and reused["output_dir"] == retried["output_dir"]
    assert reading["store"].core_count("synthetic-review") == 1


def test_real_reader_useful_partial_not_committed(reading):
    client = ModelBoundary(answer(remaining=["Missing operating limits"]))
    result = dr.run_directed_reading(**reading, client=client)
    assert result["ready"] and not result["fulfilled"]
    assert result["output"]["status"] == "partial"
    assert reading["store"].committed_reading("synthetic-review", result["output"]["task_id"]) is None
    retained = dr.run_directed_reading(**reading, client=client)
    assert retained["output"]["status"] == "partial" and not retained["reused"]
    assert len(client.calls) == 1


def test_string_question_builds_executable_contract():
    request = dr.build_directed_request(review_id="test", topic="Topic", chapter={"chapter_id": "C01", "title": "Chapter"},
        questions=["How does it operate?"], required_outputs=[{"output_id": "O01", "output_type": "explanation", "description": "Explain operation"}])
    assert request["questions"][0]["purpose"] == "How does it operate?"
    assert request["questions"][0]["required_output_ids"] == ["O01"]


def test_historical_false_commit_empty_can_retry_and_remains_inspectable(reading):
    client = ModelBoundary(answer(""), answer())
    old = dr.run_directed_reading(**reading, client=client)
    task_id = old["output"]["task_id"]
    reading["store"].commit_reading(review_id="synthetic-review", task_id=task_id, output_dir=old["output_dir"], source_hash="", gap_keys=["mechanism"])
    retried = dr.run_directed_reading(**reading, client=client, retry_empty_result=True)
    assert retried["fulfilled"] and len(client.calls) == 2
    with reading["store"]._connect() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM directed_readings WHERE task_id=?", (task_id,))]
    assert {row["status"] for row in rows} == {"unmet", "committed"}
    assert len({row["reading_key"] for row in rows}) == 2


def test_historical_partial_commit_is_demoted_without_call(reading):
    client = ModelBoundary(answer(remaining=["Unresolved synthetic boundary"]))
    old = dr.run_directed_reading(**reading, client=client)
    task_id = old["output"]["task_id"]
    reading["store"].commit_reading(review_id="synthetic-review", task_id=task_id, output_dir=old["output_dir"], source_hash="", gap_keys=["mechanism"])
    result = dr.run_directed_reading(**{**reading, "output_dir": Path(old["output_dir"]).parent / "another-location"}, client=client)
    assert result["ready"] and not result["fulfilled"] and len(client.calls) == 1
    assert result["output_dir"] == old["output_dir"]
    assert reading["store"].committed_reading("synthetic-review", task_id) is None


@pytest.mark.parametrize("row", [
    {"question_id": "Q01", "explanation": "Useful background", "availability": "unavailable"},
    {"question_id": "Q01", "explanation": "Useful background", "status": "partial"},
    {"question_id": "unrequested", "explanation": "Useful background"},
])
def test_explicit_or_unrequested_partial_is_not_fulfilled(row):
    assert dr.practical_result_status({"content": {"question_material": [row]}}, [{"question_id": "Q01"}]) == "partial"


def test_plain_prose_and_missing_question_are_partial():
    assert dr.practical_result_status({"plain_text": "Useful synthetic prose"}, [{"question_id": "Q01"}]) == "partial"
    assert dr.practical_result_status({"content": answer()}, [{"question_id": "Q01"}, {"question_id": "Q02"}]) == "partial"
    assert dr.practical_result_status({"content": answer(), "current_question_material": []}, [{"question_id": "Q01"}]) == "partial"


def test_same_paper_concurrent_tasks_get_distinct_outputs(reading):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    barrier = Barrier(2)
    calls = []
    def client(messages, **kwargs):
        calls.append(kwargs["call_id"])
        barrier.wait(timeout=5)
        return {"content": answer()}
    changed = json.loads(json.dumps(reading["request"]))
    changed["questions"][0]["question"] = "What changes the mechanism?"
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(dr.run_directed_reading, **{**reading, "request": request}, client=client)
            for request in [reading["request"], changed]]
        results = [future.result() for future in futures]
    assert len(calls) == 2
    assert results[0]["output_dir"] != results[1]["output_dir"]
    for result in results:
        stored = json.loads((Path(result["output_dir"]) / "INPUT.json").read_text())
        assert stored["task_id"] == result["output"]["task_id"]
    assert reading["store"].core_count("synthetic-review") == 1
    assert set(reading["store"].paper("synthetic-review", "synthetic-paper")["task_ids"]) == {result["output"]["task_id"] for result in results}


def test_same_task_concurrent_runs_call_boundary_once(reading):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered, release = Event(), Event()
    calls = []
    def client(messages, **kwargs):
        calls.append(kwargs["call_id"])
        entered.set()
        assert release.wait(timeout=5)
        return {"content": answer()}
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(dr.run_directed_reading, **reading, client=client)
        assert entered.wait(timeout=5)
        try:
            with pytest.raises(dr.AdmissionError, match="running_elsewhere"):
                dr.run_directed_reading(**reading, client=client)
        finally:
            release.set()
        assert first.result()["fulfilled"]
    assert len(calls) == 1


def test_bound_raw_without_result_resumes_or_explicitly_retries(reading):
    client = ModelBoundary(answer(""), answer())
    old = dr.run_directed_reading(**reading, client=client)
    directory = Path(old["output_dir"])
    old_raw = (directory / "RAW_RESPONSE.json").read_bytes()
    (directory / "DIRECTED_READING.json").unlink()
    retried = dr.run_directed_reading(**{**reading, "output_dir": directory.parent / "elsewhere"}, client=client, retry_empty_result=True)
    assert retried["fulfilled"] and len(client.calls) == 2
    assert (directory / "RAW_RESPONSE.json").read_bytes() == old_raw


def test_foreign_unbound_raw_is_not_consumed(reading):
    root = Path(reading["output_dir"])
    root.mkdir()
    raw = json.dumps({"content": answer("Wrong task cached content")})
    (root / "RAW_RESPONSE.json").write_text(raw)
    client = ModelBoundary(answer())
    result = dr.run_directed_reading(**reading, client=client)
    assert len(client.calls) == 1 and Path(result["output_dir"]) != root
    assert (root / "RAW_RESPONSE.json").read_text() == raw


def test_cli_explicit_retry_flag():
    args = dr._cli_parser().parse_args(["run", "--request", "synthetic.json", "--paper", "paper.json", "--snapshot", "snapshot", "--store", "test-only.sqlite", "--output-dir", "result", "--retry-empty-result"])
    assert args.retry_empty_result is True


@pytest.mark.parametrize("flag", [{"status": "partial"}, {"material_ready": False}, {"remaining_gap": "Missing condition"}, {"open_questions": ["Missing condition"]}])
def test_model_top_level_incomplete_preserves_partial(reading, flag):
    client = ModelBoundary({**answer(), **flag})
    result = dr.run_directed_reading(**reading, client=client)
    assert result["ready"] and not result["fulfilled"]
    assert result["output"]["status"] == "partial" and len(client.calls) == 1


def test_committed_store_location_is_still_immutable(reading):
    result = dr.run_directed_reading(**reading, client=ModelBoundary(answer()))
    with pytest.raises(dr.AdmissionError, match="reading_commit_is_immutable"):
        reading["store"].commit_reading(review_id="synthetic-review", task_id=result["output"]["task_id"], output_dir="different-location", source_hash=reading["store"].task(result["output"]["task_id"])["source_hash"], gap_keys=["mechanism"])
    assert reading["store"].summary("synthetic-review")["reading_count"] == 1


def test_stale_incomplete_observation_cannot_demote_completed_retry(reading):
    client = ModelBoundary(answer(""), answer())
    old = dr.run_directed_reading(**reading, client=client)
    # Capture the old observation, then interleave a fully real retry before
    # its delayed invalidation. No store/reader replacement is used.
    old_artifact = json.loads((Path(old["output_dir"]) / "DIRECTED_READING.json").read_text())
    assert dr.practical_result_status(old_artifact, reading["request"]["questions"]) == "unmet"
    retried = dr.run_directed_reading(**reading, client=client, retry_empty_result=True)
    invalidated = reading["store"].retain_incomplete(old["output"]["task_id"], status="unmet", output_dir=old["output_dir"])
    assert invalidated is False
    result = dr.run_directed_reading(**reading, client=client)
    assert result["fulfilled"] and result["output_dir"] == retried["output_dir"]
    assert reading["store"].task(old["output"]["task_id"])["status"] == "committed"
    assert reading["store"].committed_reading("synthetic-review", old["output"]["task_id"])["output_dir"] == retried["output_dir"]
    assert len(client.calls) == 2


def test_scalar_question_is_one_executable_question(reading):
    request = dr.build_directed_request(review_id="test", topic="Topic", chapter={"chapter_id": "C01", "title": "Chapter"},
        questions="How does it operate?", required_outputs=[{"output_id": "O01", "output_type": "explanation", "description": "Explain operation"}])
    assert len(request["questions"]) == 1
    assert request["questions"][0]["question"] == "How does it operate?"
    added = reading["store"].add_task(review_id="synthetic-review", paper_id="synthetic-paper", questions="How does it operate?", required_outputs=request["required_outputs"], gap_keys=[])
    assert len(reading["store"].task(added["task_id"])["questions"]) == 1


def test_conflicting_task_owned_output_never_overwritten(reading):
    questions, outputs, gaps = dr._practical_task_rows(reading["request"], None)
    _, messages, _, _, _, _ = dr._practical_reading_plan(
        **{k: reading[k] for k in ("request", "paper", "snapshot_dir")}, task=None,
        max_input_tokens=dr.DEFAULT_MAX_INPUT_TOKENS)
    added = reading["store"].add_task(review_id="synthetic-review", paper_id="synthetic-paper", questions=questions, required_outputs=outputs, gap_keys=gaps,
        source_hash=dr.sha256_value({"source_hash": dr.sha256_value(dr.load_practical_material(reading["snapshot_dir"])),
                                     "paper_identity": {"canonical_paper_id": "synthetic-paper", "title": "Synthetic fixture"},
                                     "runtime_config": dr.directed_reader_runtime_config(), "prompt_sha256": dr.sha256_value(messages)}))
    output = Path(reading["output_dir"]) / added["task_id"]
    output.mkdir(parents=True)
    old = json.dumps({"task_id": "foreign-task", "workflow": "practical_materials", "content": answer()})
    (output / "DIRECTED_READING.json").write_text(old)
    client = ModelBoundary(answer())
    with pytest.raises(dr.DirectedReadingError, match="output_directory_task_identity_conflict"):
        dr.run_directed_reading(**reading, client=client)
    assert not client.calls
    assert (output / "DIRECTED_READING.json").read_text() == old


def test_output_creation_failure_releases_claim_without_model_call(reading):
    Path(reading["output_dir"]).write_text("Synthetic occupied regular file")
    client = ModelBoundary(answer())
    with pytest.raises(OSError):
        dr.run_directed_reading(**reading, client=client)
    task_id = reading["store"].paper("synthetic-review", "synthetic-paper")["task_ids"][0]
    assert reading["store"].task(task_id)["status"] == "pending"
    assert reading["store"].attempts(task_id)[-1]["status"] == "failed"
    assert not client.calls


def test_interrupted_retry_saved_raw_resumes_before_older_empty(reading):
    old = dr.run_directed_reading(**reading, client=ModelBoundary(answer("")))
    old_dir = Path(old["output_dir"])
    retry_dir = old_dir / "retry-02"
    blocked = retry_dir / "DIRECTED_READING.json"
    calls = []
    def client(messages, **kwargs):
        calls.append(kwargs["call_id"])
        # Real filesystem failure after a successful boundary response: the
        # artifact destination is temporarily a directory, but RAW is writable.
        blocked.mkdir()
        return {"content": answer()}
    with pytest.raises(OSError):
        dr.run_directed_reading(**reading, client=client, retry_empty_result=True)
    raw = (retry_dir / "RAW_RESPONSE.json").read_bytes()
    blocked.rmdir()
    resumed = dr.run_directed_reading(**reading, client=ModelBoundary())
    assert resumed["fulfilled"] and resumed["output_dir"] == str(retry_dir)
    assert (retry_dir / "RAW_RESPONSE.json").read_bytes() == raw
    assert len(calls) == 1


def test_bare_question_collection_is_not_answer_content(reading):
    client = ModelBoundary({"question_material": ["How does the mechanism operate?"]})
    result = dr.run_directed_reading(**reading, client=client)
    assert not result["ready"] and not result["fulfilled"]
    assert reading["store"].summary("synthetic-review")["reading_count"] == 0


def test_directed_reader_default_kwargs_and_preflight_agree(reading):
    client = ModelBoundary(answer())
    result = dr.run_directed_reading(**reading, client=client)
    runtime_config = dr.directed_reader_runtime_config()
    assert runtime_config == {"model": "qwen3.7-flash", "thinking": True,
                              "max_output_tokens": 20000, "thinking_budget": 8192}
    assert all(client.calls[0][1][key] == value for key, value in runtime_config.items())
    assert result["output"]["runtime_config"] == runtime_config
    saved_input = json.loads((Path(result["output_dir"]) / "INPUT.json").read_text())
    assert saved_input["runtime_config"] == runtime_config
    preflight = dr.preflight_directed_reading(**{k: reading[k] for k in ("request", "paper", "snapshot_dir")})
    assert preflight["runtime_config"] == runtime_config
    assert preflight["thinking_budget"] == 8192 and preflight["max_output_tokens"] == 20000


@pytest.mark.parametrize("changed", [{"thinking_budget": 16384}, {"max_output_tokens": 24000}, {"model": "qwen3.5-plus"}])
def test_directed_fulfilled_cache_changes_with_effective_runtime_keeps_history(reading, changed):
    client = ModelBoundary(answer(), answer("A new higher-capacity answer"))
    first = dr.run_directed_reading(**reading, client=client)
    old_root = Path(first["output_dir"])
    frozen = {p: p.read_bytes() for p in old_root.rglob("*") if p.is_file()}
    second = dr.run_directed_reading(**reading, client=client, **changed)
    assert len(client.calls) == 2 and second["fulfilled"] and not second["reused"]
    assert first["output"]["task_id"] != second["output"]["task_id"]
    assert second["output_dir"] != first["output_dir"]
    assert all(client.calls[-1][1][key] == value for key, value in changed.items())
    assert all(p.read_bytes() == data for p, data in frozen.items())
    assert dr.run_directed_reading(**reading, client=client, **changed)["reused"]
    assert dr.run_directed_reading(**reading, client=client)["output_dir"] == first["output_dir"]
    assert len(client.calls) == 2 and reading["store"].core_count("synthetic-review") == 1
    assert len(reading["store"].readings("synthetic-review", "synthetic-paper")) == 2


@pytest.mark.parametrize("proof", ["current", "missing", "old_budget"])
def test_directed_legacy_reuse_requires_matching_budget_proof_preserves_artifact(reading, proof):
    _, messages, _, questions, outputs, gaps = dr._practical_reading_plan(
        **{k: reading[k] for k in ("request", "paper", "snapshot_dir")}, task=None,
        max_input_tokens=dr.DEFAULT_MAX_INPUT_TOKENS)
    legacy = reading["store"].add_task(review_id="synthetic-review", paper_id="synthetic-paper",
        questions=questions, required_outputs=outputs, gap_keys=gaps, source_hash="")
    old_root = Path(reading["output_dir"]) / "legacy"
    old_root.mkdir(parents=True)
    artifact = {"workflow": "practical_materials", "task_id": legacy["task_id"], "paper_id": "synthetic-paper",
                "content": answer(), "question_material": answer()["question_material"]}
    if proof != "missing":
        artifact["runtime_config"] = dr.directed_reader_runtime_config(thinking_budget=4096 if proof == "old_budget" else 8192)
    (old_root / "DIRECTED_READING.json").write_text(json.dumps(artifact))
    (old_root / "PROMPT.json").write_text(json.dumps({"messages": messages}))
    reading["store"].commit_reading(review_id="synthetic-review", task_id=legacy["task_id"], output_dir=str(old_root), source_hash="", gap_keys=gaps)
    frozen = {p: p.read_bytes() for p in old_root.iterdir() if p.is_file()}
    client = ModelBoundary(answer())
    result = dr.run_directed_reading(**reading, client=client)
    assert result["fulfilled"]
    assert result["reused"] is (proof == "current")
    assert len(client.calls) == (0 if proof == "current" else 1)
    assert all(p.read_bytes() == data for p, data in frozen.items())


def test_directed_floor_keys_effective_answer_limit(reading):
    client = ModelBoundary(answer())
    first = dr.run_directed_reading(**reading, client=client, max_output_tokens=1)
    second = dr.run_directed_reading(**reading, client=client, max_output_tokens=64)
    assert second["reused"] and second["output_dir"] == first["output_dir"]
    assert client.calls[0][1]["max_output_tokens"] == 64
    assert first["output"]["runtime_config"]["max_output_tokens"] == 64


def test_directed_cli_defaults_and_override_model():
    parser = dr._cli_parser()
    for command in ("run", "preflight"):
        args = [command, "--request", "r", "--paper", "p", "--snapshot", "s"]
        if command == "run":
            args.extend(["--store", "store", "--output-dir", "out"])
        defaults = parser.parse_args(args)
        assert defaults.max_output_tokens == 20000 and defaults.thinking_budget == 8192
        assert parser.parse_args([*args, "--model", "qwen3.5-plus"]).model == "qwen3.5-plus"


@pytest.mark.parametrize("model,json_mode", [("qwen3.7-flash", True), ("qwen3.5-plus", False)])
def test_live_reader_constructor_receives_effective_model_budget(reading, monkeypatch, tmp_path, model, json_mode):
    constructors, calls = [], []
    metadata = {"effective_request": {"model": model, "answer_tokens": 24000, "thinking_budget": 16384},
                "cap_pressure": {"thinking": True}}

    class Client:
        def __init__(self, **kwargs):
            constructors.append(kwargs)

        def complete(self, messages, **kwargs):
            calls.append(kwargs)
            return {"content": answer(), **metadata}

    monkeypatch.setattr(dr, "QwenDirectClient", Client)
    key = tmp_path / "fixture-not-a-real-key.txt"
    key.write_text("synthetic-test-placeholder")
    result = dr.run_directed_reading(**reading, key_file=key, budget_ledger_path=tmp_path / "ledger.sqlite",
        budget_limit_cny=100, model=model, max_output_tokens=24000, thinking_budget=16384)
    assert len(constructors) == len(calls) == 1
    for item in (constructors[0], calls[0]):
        assert item["model"] == model
        assert item["max_output_tokens"] == 24000 and item["thinking_budget"] == 16384
    assert constructors[0]["json_mode"] is json_mode
    assert all(result["output"][key] == value for key, value in metadata.items())


def test_interrupted_reader_raw_only_reuses_exact_runtime(reading):
    blocked = []

    def interrupted(messages, **kwargs):
        task_id = kwargs["call_id"].split(":")[-2]
        artifact_path = Path(reading["output_dir"]) / task_id / "DIRECTED_READING.json"
        artifact_path.mkdir()
        blocked.append(artifact_path)
        return {"content": answer()}

    with pytest.raises(OSError):
        dr.run_directed_reading(**reading, client=interrupted)
    old_root = blocked[0].parent
    old_raw = (old_root / "RAW_RESPONSE.json").read_bytes()
    new_client = ModelBoundary(answer("New budget answer"))
    changed = dr.run_directed_reading(**reading, client=new_client, thinking_budget=16384)
    assert len(new_client.calls) == 1 and changed["fulfilled"]
    assert changed["output_dir"] != str(old_root)
    assert (old_root / "RAW_RESPONSE.json").read_bytes() == old_raw
    blocked[0].rmdir()
    same_budget_client = ModelBoundary()
    resumed = dr.run_directed_reading(**reading, client=same_budget_client)
    assert resumed["fulfilled"] and resumed["output_dir"] == str(old_root)
    assert not same_budget_client.calls
