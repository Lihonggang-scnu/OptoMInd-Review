"""Offline process/cache boundaries for the resident production entry."""
import json
import os
import sys
import types

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
