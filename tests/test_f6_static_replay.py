"""F6 gates G7/G8: static replay export correctness and sanitisation."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.export_static_replay as exporter


def _stamp(offset):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + offset)) + "Z"


def _make_run(tmp_path: Path) -> Path:
    run = tmp_path / "rhr_replaydemo01"
    run.mkdir(parents=True)
    rows = [
        {"timestamp": _stamp(0), "event": "run_started", "entry_mode": "cli"},
        {"timestamp": _stamp(1), "event": "stage_started", "stage": "topic_scoped_kb"},
        {"timestamp": _stamp(2), "event": "progress_note", "stage": "topic_scoped_kb",
         "note": "working dir C:\\Users\\Administrator\\x and key sk-abcd123456 under api_keys\\qwen-api-key.txt on HOST-DESKTOP01"},
        {"timestamp": _stamp(3), "event": "paper_fetched", "stage": "s2_literature_intelligence",
         "file": "10.1021/acsphotonics.3c00572.pdf"},
    ]
    giant = {"timestamp": _stamp(3.5), "event": "stage_finished", "stage": "topic_scoped_kb",
             "status": "completed", "cost_cny": 0.1,
             "selection": [{"blob": "x" * (3 * 1024 * 1024)}]}
    rows.insert(4, giant)
    rows.append({"timestamp": _stamp(4), "event": "stage_finished", "stage": "topic_scoped_kb",
                 "status": "completed", "cost_cny": 0.5, "wall_time_seconds": 12.0})
    with open(run / "HARNESS_EVENTS.jsonl", "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + chr(10))
    (run / "HARNESS_COST.json").write_text(
        json.dumps({"cost_cny": 0.6, "global_cost_budget_cny": 120.0, "model_call_count": 7}),
        encoding="utf-8",
    )
    return run


def test_sanitize_masks_every_forbidden_class(monkeypatch):
    monkeypatch.setattr(exporter, "_machine_names", lambda: ["HOST-DESKTOP01", os.environ.get("COMPUTERNAME", ""), os.environ.get("USERNAME", "")])
    text = (
        "see C:\\Users\\Administrator\\report.docx and F:\\OptoMind\\data.py " +
        "key sk-ZZ9900xx at api_keys/qwen-api-key.txt on HOST-DESKTOP01 " +
        "paper 10.1021/acsphotonics.3c00572.pdf plus notes.pdf"
    )
    masked = exporter.sanitize_text(text)
    lowered = masked.lower()
    for forbidden in ("api_keys", "c:\\users", "f:\\optomind", "sk-zz9900xx", "acsphotonics", "notes.pdf", "host-desktop01"):
        assert forbidden not in lowered, forbidden


def test_export_bundle_is_sanitized_and_bounded(tmp_path: Path):
    run = _make_run(tmp_path)
    out = tmp_path / "replay" / "demo"
    code = exporter.export(run, out)
    assert code == 0
    payload = (out / "replay-data.json").read_bytes()
    assert len(payload) <= exporter.HARD_CAP_BYTES
    text = payload.decode("utf-8")
    # G8 assertions -- pasted verbatim into the work log:
    lowered = text.lower()
    assert "api_keys" not in lowered
    assert "c:\\users" not in lowered
    assert "sk-" not in lowered
    assert "acsphotonics" not in lowered
    machine = os.environ.get("COMPUTERNAME", "")
    if machine:
        assert machine.lower() not in lowered
    assert ".pdf" not in lowered.replace("[pdf文件名]", "")
    index_html = (out / "index.html").read_text(encoding="utf-8")
    assert '"/assets/' not in index_html
    assert 'data-optomind-mode="static-replay"' in index_html
    fallback_html = (out / "404.html").read_text(encoding="utf-8")
    assert '"/assets/' not in fallback_html
    assert 'data-optomind-mode="static-replay"' in fallback_html
    assert (out / ".nojekyll").is_file()
    data = json.loads(text)
    assert data["timeline"][0]["event"] == "run_started"
    assert data["timeline"][-1]["event"] == "stage_finished"
    assert "snapshot" in data
    assert data["snapshot"]["artifacts"][0]["kind"] == "english_publication"
    # The static bundle must not turn a generated-artifact label into a raw
    # publisher filename or an absolute path.
    assert "f:\\optimind" not in json.dumps(data).lower()


def test_giant_line_is_clipped_not_leaked(tmp_path: Path):
    run = _make_run(tmp_path)
    out = tmp_path / "replay" / "giant"
    assert exporter.export(run, out) == 0
    raw = (out / "replay-data.json").read_bytes()
    assert len(raw) < 512 * 1024
    assert "xxxx" not in raw.decode("utf-8")


def test_withheld_payload_is_reported_with_sizes_only(tmp_path: Path):
    # The 3 MB `selection` field is DROPPED by whitelist rather than clipped,
    # so before this the bundle showed no sign a payload had been withheld.
    run = _make_run(tmp_path)
    out = tmp_path / "replay" / "withheld"
    assert exporter.export(run, out) == 0
    data = json.loads((out / "replay-data.json").read_text(encoding="utf-8"))
    flagged = [line for line in data["timeline"] if line.get("withheld_bytes")]
    assert len(flagged) == 1, [line.get("event") for line in flagged]
    assert flagged[0]["withheld_bytes"] > 3 * 1024 * 1024
    assert flagged[0]["withheld_fields"] == 1
    # sizes only: no byte of the withheld blob may reach the bundle
    assert "xxxx" not in json.dumps(data, ensure_ascii=False)


def test_small_drops_do_not_badge_every_row(tmp_path: Path):
    # Reference run: flagging sub-4 KiB drops badged 41 of 49 rows.
    run = _make_run(tmp_path)
    out = tmp_path / "replay" / "quiet"
    assert exporter.export(run, out) == 0
    data = json.loads((out / "replay-data.json").read_text(encoding="utf-8"))
    badged = sum(1 for line in data["timeline"] if line.get("withheld_bytes"))
    assert badged * 2 < len(data["timeline"])
