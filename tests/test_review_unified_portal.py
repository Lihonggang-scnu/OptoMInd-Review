from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import quickstart
from review_portal.local_runtime import LocalPortalServer, LocalRuntimeController


ROOT = Path(__file__).resolve().parents[1]


def _controller(tmp_path: Path) -> LocalRuntimeController:
    return LocalRuntimeController(
        project_root=ROOT,
        key_dir=tmp_path / "keys",
        local_run_root=tmp_path / "runs",
        runtime_preparer=lambda: Path("python"),
        quick_command_builder=lambda *args, **kwargs: ["python"],
        full_command_builder=lambda *args, **kwargs: ["python"],
    )


def test_public_replay_contains_three_runs_and_thirteen_stage_projection() -> None:
    manifest = json.loads((ROOT / "replay" / "replay-manifest.json").read_text(encoding="utf-8"))
    script = (ROOT / "replay" / "assets" / "review.js").read_text(encoding="utf-8")
    config = (ROOT / "replay" / "assets" / "config.js").read_text(encoding="utf-8")
    assert len(manifest["runs"]) == 3
    assert script.count("{n:") == 13
    assert 'productMode: "review"' in config
    assert "liveEnabled: false" in config
    for run in manifest["runs"]:
        assert (ROOT / "replay" / run["data_path"]).is_file()


def test_quick_and_full_commands_keep_question_out_of_command_line(tmp_path: Path) -> None:
    question = tmp_path / "question.txt"
    run_dir = tmp_path / "run"
    key = tmp_path / "qwen-api-key.txt"
    quick = quickstart.quick_command("python", question_file=question, run_dir=run_dir, qwen_file=key)
    full = quickstart.full_command("python", question_file=question, run_dir=run_dir, qwen_file=key)
    assert "--question-file" in quick and str(question.resolve()) in quick
    assert "--question" not in quick
    assert "--global-budget-cny" in quick and "3.0" in quick
    assert "--no-latex-publication" in quick
    assert "--global-budget-cny" in full and "15.0" in full
    assert "--visual-review-auto-accept-seconds" in full


def test_local_server_enables_live_mode_and_serves_public_artifact(tmp_path: Path) -> None:
    server = LocalPortalServer(
        ("127.0.0.1", 0),
        project_root=ROOT,
        ui_root=ROOT / "replay",
        controller=_controller(tmp_path),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        config = urllib.request.urlopen(f"{base}/assets/config.js", timeout=5).read().decode("utf-8")
        status = json.loads(urllib.request.urlopen(f"{base}/api/local/status", timeout=5).read())
        summary = json.loads(
            urllib.request.urlopen(
                f"{base}/artifacts/e2e/03-scalable-photonic-computing/run-summary.json",
                timeout=5,
            ).read()
        )
        assert "liveEnabled:true" in config
        assert status["live_enabled"] is True
        assert summary["english_pdf"]["pages"] == 44
        try:
            urllib.request.urlopen(f"{base}/artifacts/../api_keys/qwen-api-key.txt", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code in {403, 404}
        else:
            raise AssertionError("path traversal must not expose credentials")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_pages_workflow_publishes_only_replay_and_portable_artifacts() -> None:
    workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    assert "cp -R replay/. _site/" in workflow
    assert "cp -R artifacts/e2e _site/artifacts/" in workflow
    assert "outputs/" not in workflow
    assert "api_keys" not in workflow
