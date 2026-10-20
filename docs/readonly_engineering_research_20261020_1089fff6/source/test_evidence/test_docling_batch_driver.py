"""Offline process/cache boundaries for the resident production entry."""
import json
import logging
import os
import sys
import types
from argparse import Namespace

import pytest

from scripts.upgrade3 import parse_docling_batch as batch


def test_bad_pdf_isolated_and_identical_content_deduplicated(tmp_path, monkeypatch):
    class Pdf:
        def __init__(self, path):
            if not open(path, "rb").read().startswith(b"%PDF"): raise ValueError("broken PDF")
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def __len__(self): return 2
    monkeypatch.setitem(sys.modules, "pypdfium2", types.SimpleNamespace(PdfDocument=Pdf))
    for name, data in (("good1", b"%PDF one"), ("good2", b"%PDF two"), ("alias", b"%PDF one"), ("bad", b"broken")):
        (tmp_path / f"{name}.pdf").write_bytes(data)
    manifest = tmp_path / "M.json"
    manifest.write_text(json.dumps({"records": [{"local_pdf": f"{name}.pdf", "canonical_paper_id": name} for name in ("good1", "bad", "good2", "alias", "missing")]}))
    rows, aliases, failures = batch.normalize(manifest)
    assert len(rows) == 2 and len(aliases) == 5 and len(failures) == 2
    assert aliases[0]["label"] == aliases[3]["label"]
    config = tmp_path / "config.json"; config.write_text("{}")
    monkeypatch.setattr(batch, "cached", lambda args, row, config: {"status": "success", "source_sha256": row["pdf_sha256"], "label": row["label"]})
    monkeypatch.setattr(batch, "gpu_allocator", lambda *a: pytest.fail("a warm valid batch must not start a GPU worker"))
    output = tmp_path / "out"
    assert batch.main(["--mode", "basic", "--manifest", str(manifest), "--config", str(config), "--output-root", str(output), "--resume"]) == 2
    report = batch.read(output / "BATCH_RESULT.json")
    assert report["cached_count"] == 2 and report["attempted_count"] == report["reconverted_count"] == 0
    assert len(report["records"]) == 4


def test_stale_lock_is_retained_and_live_pid_blocks_duplicate(tmp_path, monkeypatch):
    lock = tmp_path / ".batch.lock"; lock.write_text(json.dumps({"pid": 99999999}))
    monkeypatch.setattr(batch, "process_alive", lambda pid: pid == os.getpid())
    batch.acquire_lock(lock)
    assert len(list(tmp_path.glob(".batch.lock.stale-*"))) == 1
    with pytest.raises(ValueError, match="active_pid"):
        batch.acquire_lock(lock)


def test_allocator_reserves_background_and_512mib():
    assert batch.allocator_fraction(10240, 1637) == pytest.approx((8192 - 1637 - 512) / 10240)
    assert batch.allocator_fraction(10240, 6000) == pytest.approx((8192 - 6000 - 512) / 10240)
    with pytest.raises(ValueError, match="headroom"):
        batch.allocator_fraction(10240, 8000)


def test_basic_default_uses_single_thread_profile_and_bounded_child():
    args = Namespace(mode="basic", safe_parser=False)
    assert batch.basic_safe_parser({}, args)
    assert batch.batch_worker_count({}, args) == 1
    assert batch.batch_worker_count({"workers": 3}, args) == 3
    assert batch.worker_batch_limit({}, args) == 20
    assert batch.worker_batch_limit({"max_documents_per_worker": 7}, args) == 7
    assert batch.worker_batch_limit({"max_documents_per_worker": 7}, Namespace(mode="formula", safe_parser=False)) is None


def recovery_setup(tmp_path, monkeypatch):
    args = Namespace(output_root=tmp_path, mode="basic", resume=True, execution_id="")
    rows = [{"label": name, "pdf_sha256": name} for name in ("a", "b")]
    monkeypatch.setattr(batch, "gpu_allocator", lambda config: .5)
    def cache(args, row, config):
        path = args.output_root / row["label"] / "run.json"
        value = batch.read(path) if path.exists() else None
        return value if value and value["status"] == "success" else None
    monkeypatch.setattr(batch, "cached", cache)
    return args, rows


def test_native_restart_reuses_success_and_preserves_original_culprit(tmp_path, monkeypatch):
    args, rows = recovery_setup(tmp_path, monkeypatch); calls = []
    def run(command, root, config, started, samples, entry):
        calls.append(entry["labels"])
        for label in entry["labels"]:
            batch.atomic(root / label / "run.json", {"label": label, "execution_id": entry["execution_id"],
                "status": "started" if len(calls) == 1 and label == "b" else "success"})
        code = 0xC0000374 if len(calls) == 1 else 0
        entry.update(returncode=code); return code, None
    monkeypatch.setattr(batch, "run_isolated_worker", run)
    code, aborted, history, _ = batch.recover_workers(args, rows, {}, batch.time.monotonic(), [], [])
    assert code == 0 and aborted is None and calls == [["a", "b"], ["b"]]
    assert history[0]["returncode"] == 0xC0000374 and history[0]["started_culprits"] == ["b"]
    assert batch.read(tmp_path / "WORKER_HISTORY.json")[0]["receipts"][1]["status"] == "started"


def test_repeated_culprit_isolated_then_one_safe_parser_attempt(tmp_path, monkeypatch):
    args, rows = recovery_setup(tmp_path, monkeypatch); calls = []
    def run(command, root, config, started, samples, entry):
        calls.append((entry["labels"], entry["safe_parser"]))
        if entry["safe_parser"] or len(calls) == 3:
            for label in entry["labels"]: batch.atomic(root / label / "run.json", {"label": label, "status": "success", "execution_id": entry["execution_id"]})
            code = 0
        else:
            batch.atomic(root / "a" / "run.json", {"label": "a", "status": "started", "execution_id": entry["execution_id"]})
            code = 0xC0000005
        entry.update(returncode=code); return code, None
    monkeypatch.setattr(batch, "run_isolated_worker", run)
    code, aborted, history, _ = batch.recover_workers(args, rows, {"safe_parser": False}, batch.time.monotonic(), [], [])
    assert code == 0 and aborted is None
    assert calls == [(["a", "b"], False), (["a", "b"], False), (["b"], False), (["a"], True)]
    assert len(history) == 4


@pytest.mark.parametrize("code,reason", [(1, None), (0xC0000374, "batch_deadline_exceeded"), (0xC0000374, "whole_gpu_memory_ceiling_exceeded")])
def test_non_native_and_controlled_stop_never_retry(tmp_path, monkeypatch, code, reason):
    args, rows = recovery_setup(tmp_path, monkeypatch); calls = []
    def run(command, root, config, started, samples, entry):
        calls.append(entry); entry.update(returncode=code); return code, reason
    monkeypatch.setattr(batch, "run_isolated_worker", run)
    result = batch.recover_workers(args, rows, {}, batch.time.monotonic(), [], [])
    assert result[:2] == (code, reason) and len(calls) == 1


def test_zero_exit_without_receipt_stops_instead_of_reloading_models(tmp_path, monkeypatch):
    args, rows = recovery_setup(tmp_path, monkeypatch); calls = []
    def run(command, root, config, started, samples, entry):
        calls.append(entry); entry.update(returncode=0); return 0, None
    monkeypatch.setattr(batch, "run_isolated_worker", run)
    result = batch.recover_workers(args, rows, {}, batch.time.monotonic(), [], [])
    assert result[:2] == (0, "worker_no_progress") and len(calls) == 1


def test_windows_cleanup_targets_only_owned_pid_tree(monkeypatch):
    calls = []
    class Process:
        pid = 12345
        def poll(self): return None
        def wait(self, timeout): calls.append(("wait", timeout))
    monkeypatch.setattr(batch.os, "name", "nt")
    monkeypatch.setattr(batch.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(batch.subprocess, "run", lambda command, **kwargs: calls.append(command) or Namespace(returncode=0))
    batch.stop_owned_worker(Process())
    assert calls == [["taskkill", "/PID", "12345", "/T", "/F"], ("wait", 15)]


def test_formula_oom_adapts_batches_and_fails_closed_on_other_errors():
    logger = logging.getLogger(batch.FORMULA_MODEL_LOGGER_NAME)

    class Cuda:
        def __init__(self):
            self.empty_cache_calls = 0

        def is_available(self):
            return True

        def empty_cache(self):
            self.empty_cache_calls += 1

    torch = types.SimpleNamespace(cuda=Cuda())

    def make_elements(count):
        return [types.SimpleNamespace(item=types.SimpleNamespace(self_ref=f"#/texts/{i}", text="raw"))
                for i in range(count)]

    class OomThenSuccess:
        def __init__(self):
            self.calls = []

        def __call__(self, *, doc, element_batch):
            batch = list(element_batch)
            self.calls.append(len(batch))
            if len(self.calls) == 1:
                logger.error("Error processing code/formula batch: CUDA out of memory (synthetic)")
                yield from (element.item for element in batch)
                return
            for element in batch:
                element.item.text = f"latex-{element.item.self_ref}"
                yield element.item

    elements = make_elements(4)
    model = OomThenSuccess()
    processed, trace, retries, error = batch._run_formula_batches(model, object(), elements, 4, torch)
    assert processed == [f"#/texts/{i}" for i in range(4)]
    assert model.calls == [4, 2, 2]
    assert [entry["status"] for entry in trace] == ["oom", "success", "success"]
    assert retries == 1 and not error and torch.cuda.empty_cache_calls == 1

    class AlwaysOom:
        def __init__(self):
            self.calls = []

        def __call__(self, *, doc, element_batch):
            batch = list(element_batch)
            self.calls.append(len(batch))
            logger.error("Error processing code/formula batch: CUDA out of memory (synthetic)")
            yield from (element.item for element in batch)

    elements = make_elements(2)
    model = AlwaysOom()
    processed, trace, retries, error = batch._run_formula_batches(model, object(), elements, 2, torch)
    assert processed == []
    assert model.calls == [2, 1]
    assert trace[-1]["status"] == "oom_terminal"
    assert error.startswith("formula_cuda_oom_at_batch_size_1:")
    assert retries == 1

    class NonOomError:
        def __init__(self):
            self.calls = []

        def __call__(self, *, doc, element_batch):
            batch = list(element_batch)
            self.calls.append(len(batch))
            logger.error("Error processing code/formula batch: synthetic decoder failure")
            yield from (element.item for element in batch)

    elements = make_elements(3)
    model = NonOomError()
    processed, trace, retries, error = batch._run_formula_batches(model, object(), elements, 3, torch)
    assert processed == []
    assert model.calls == [3]
    assert trace[0]["status"] == "error"
    assert error.startswith("formula_model_error:")
    assert retries == 0 and all(element.item.text == "raw" for element in elements)
