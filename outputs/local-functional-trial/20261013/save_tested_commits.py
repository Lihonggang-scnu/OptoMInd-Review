"""Commit frozen increment and tested local adaptations separately, without push."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timedelta, timezone

ROOT = Path(r"D:\OptoMind-Article-3")
RUN = ROOT / "local-functional-trial/20261013"
PREVIOUS = ROOT / "local-integration/20261013"
RUN.mkdir(parents=True, exist_ok=True)
(RUN / "logs").mkdir(exist_ok=True)
(RUN / "commit-messages").mkdir(exist_ok=True)
os.environ["GIT_OPTIONAL_LOCKS"] = "0"
acceptance = json.loads((PREVIOUS / "LOCAL_ACCEPTANCE.json").read_text(encoding="utf-8"))
event_path = RUN / "logs/commit-commands.json"
events = json.loads(event_path.read_text(encoding="utf-8")) if event_path.exists() else []
results = []


def git(repo, *args):
    process = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "user.name=Codex",
                              "-c", "user.email=codex@local", "-C", str(repo), *args],
                             capture_output=True, timeout=120)
    record = {"repository": str(repo), "args": list(args), "exit_code": process.returncode,
              "stdout": process.stdout.decode("utf-8", errors="replace"),
              "stderr": process.stderr.decode("utf-8", errors="replace")}
    events.append(record)
    (RUN / "logs/commit-commands.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    if process.returncode:
        raise RuntimeError(f"Git operation failed: {args!r}; full log retained")
    return record["stdout"].strip()


for entry in acceptance["repositories"]:
    repo = Path(entry["repository"])
    assert git(repo, "rev-parse", "HEAD") == entry["base_head"]
    assert git(repo, "branch", "--show-current") == entry["branch"]
    assert git(repo, "write-tree") == entry["frozen_patch_index_tree"]
    for name, digest in entry["changed_file_hashes"].items():
        assert hashlib.sha256((repo / name).read_bytes()).hexdigest() == digest
    assert set(git(repo, "diff", "--name-only").splitlines()) | set(git(repo, "ls-files", "--others", "--exclude-standard").splitlines()) == set(entry["local_paths"])

for entry in acceptance["repositories"]:
    repo = Path(entry["repository"])
    product = entry["product"]
    first_message = RUN / "commit-messages" / f"{product}-original.txt"
    first_message.write_text(
        f"feat: apply original frozen {product} increment (2026-10-12)\n\n"
        f"Base commit: {entry['base_head']}\nFrozen handoff tree: {entry['frozen_patch_index_tree']}\n"
        "Preserve the exact supplied binary delta separately from local adaptations.\n", encoding="utf-8")
    git(repo, "commit", "--file", str(first_message))
    original_commit = git(repo, "rev-parse", "HEAD")
    assert git(repo, "rev-parse", "HEAD^{tree}") == entry["frozen_patch_index_tree"]
    git(repo, "add", "--", *entry["local_paths"])
    assert git(repo, "write-tree") == entry["tested_working_content_tree"]
    second_message = RUN / "commit-messages" / f"{product}-windows.txt"
    second_message.write_text(
        f"fix(windows): preserve tested local {product} adaptations\n\n"
        f"Tested content tree: {entry['tested_working_content_tree']}\n"
        "Record native storage/card-locator or read-only material metadata fixes.\n"
        "Validation: selected local checks recorded in local-integration/20261013/LOCAL_ACCEPTANCE.json.\n"
        "Historical replay owner-ID mismatch remains separate; no tolerance or scientific gate changes.\n", encoding="utf-8")
    git(repo, "commit", "--file", str(second_message))
    tested_commit = git(repo, "rev-parse", "HEAD")
    assert git(repo, "rev-parse", "HEAD^{tree}") == entry["tested_working_content_tree"]
    assert not git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    results.append({"product": product, "repository": str(repo), "branch": entry["branch"],
                    "original_increment_commit": original_commit, "original_increment_tree": entry["frozen_patch_index_tree"],
                    "windows_adaptation_commit": tested_commit, "tested_tree": entry["tested_working_content_tree"],
                    "tested_content_unchanged": True, "working_tree_clean": True, "pushed": False})
    (RUN / "TESTED_COMMITS.json").write_text(json.dumps({
        "timestamp": datetime.now(timezone(timedelta(hours=8))).isoformat(), "repositories": results,
        "validation": {"pytest_passed": 142, "pytest_failed_historical": 1, "cli_help_passed": 10},
        "remote_push": False, "commit_identity_source": "F:/OptoMind local Git config: Codex <codex@local>; command-local overrides only",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(results, ensure_ascii=True, indent=2))
