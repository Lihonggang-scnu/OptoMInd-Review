"""Check and index the small public preflight-consumer evidence package.

This script is local-only.  It does not access the network, credentials,
source databases, paper full text, or the original run directories.
"""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "MANIFEST.md"
MAX_BYTES = 1024 * 1024

PURPOSES = {
    "README.md": ("reading order and scope", "derived from parent acceptance records"),
    "ASTRA_ACCEPTANCE.md": ("root verdict", "parent ASTRA_ACCEPTANCE.md"),
    "LOCAL_OFFLINE_ACCEPTANCE.md": ("offline controls and selected recovery evidence", "parent records/LOCAL_OFFLINE_ACCEPTANCE.md"),
    "LOCAL_REAL_PROBE.md": ("actual F1/F3 result and limitations", "parent records/LOCAL_REAL_PROBE.md"),
    "PROVENANCE.md": ("source and omission boundary", "derived"),
    "COMMANDS.md": ("local reproduction and review commands", "derived from parent acceptance records"),
    "SECURITY_SCAN.md": ("public package scan statement", "derived"),
    "pack_public.py": ("local manifest and safety checker", "new packaging utility"),
    "real_f1/RESULT.json": ("sanitized F1 result", "runs/real_probe/f1_cache_resume/RESULT.json"),
    "real_f1/TASK_COMPARISON.json": ("fresh/resumed task comparison", "derived from F1 result"),
    "real_f1/MESSAGE_COMPARISON.json": ("writer-message hash comparison", "derived from F1 message files"),
    "real_f1/STAGE_METADATA.json": ("F1 production entrypoint metadata", "derived from PRE_FLIGHT.json and F1 result"),
    "real_f3/CONFIG.json": ("sanitized real F3 configuration", "derived from PRE_FLIGHT.json"),
    "real_f3/INPUTS.json": ("sanitized matching-input inventory", "derived from PRE_FLIGHT.json"),
    "real_f3/COST.json": ("actual shared-ledger cost summary", "derived from F3 RESULT.json"),
    "real_f3/TOOL_RESULT.md": ("AI-generated local answer projection", "derived from FORMAL_COLLECTOR_RESULT.json"),
    "real_f3/COMPACT_SUMMARY.json": ("sanitized compact handoff summary", "derived from FORMAL_COLLECTOR_RESULT.json"),
    "real_f3/COMPACT_TOOL_FEEDBACK.json": ("actual compact tool-feedback projection", "derived from COMPACT_TOOL_FEEDBACK.json"),
    "real_f3/LEVEL1_PAYLOAD_PROJECTION.json": ("question, provisional scope, and tool-feedback projection", "derived from LEVEL1_OUTLINE_PAYLOAD.json"),
    "real_f3/OUTLINE_RESPONSE.json": ("raw provider-generated outline response", "derived from LEVEL1_OUTLINE_RECORD.json"),
    "real_f3/OUTLINE_TELEMETRY.json": ("sanitized outline telemetry", "derived from LEVEL1_OUTLINE_RECORD.json"),
    "real_f3/PAYLOAD_KEYS.json": ("actual stage payload shape", "derived from LEVEL1_OUTLINE_PAYLOAD.json"),
    "real_f3/STAGE_MESSAGES.json": ("safe actual message metadata", "derived from production _messages_for output"),
    "real_f3/SYSTEM_MESSAGE.md": ("actual outline system message", "derived from production _messages_for output"),
    "real_f3/BOUNDARIES.md": ("content quality limits", "root review and actual F3 output"),
    "offline/F1_BLOCKED_CONTRACT_FAILED.json": ("selected F1 blocked-run evidence", "derived from records/local_replay/f1"),
    "offline/F3_LOCAL_RETRY_PARTIAL.json": ("selected F3 local partial evidence", "derived from records/local_replay/f23"),
}


def canonical_bytes(path: Path) -> bytes:
    """Return stable UTF-8 LF bytes, matching the Git text blob convention."""
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    except UnicodeDecodeError:
        return raw


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(canonical_bytes(path))
    return digest.hexdigest()


def files() -> list[Path]:
    return sorted(path for path in ROOT.rglob("*") if path.is_file() and path.name != "MANIFEST.md")


def scan() -> list[str]:
    problems: list[str] = []
    forbidden = [
        re.compile(r"api_keys[\\/]", re.IGNORECASE),
        re.compile(r"authorization\s*:\s*bearer", re.IGNORECASE),
        re.compile(r"bearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
        re.compile(r"(?:x-api-key|api-key|access-token)\s*[:=]\s*[^\s`]+", re.IGNORECASE),
    ]
    for path in files():
        relative = path.relative_to(ROOT).as_posix()
        size = len(canonical_bytes(path))
        if size >= MAX_BYTES:
            problems.append(f"oversize:{relative}:{size}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            problems.append(f"non_utf8:{relative}")
            continue
        for pattern in forbidden:
            if pattern.search(text):
                problems.append(f"forbidden_pattern:{relative}:{pattern.pattern}")
    return problems


def manifest_text() -> str:
    rows = [
        "# MANIFEST",
        "",
        "SHA-256 manifest for the public handoff. `MANIFEST.md` is excluded from its own rows.",
        "",
        "| relative path | purpose | source | bytes | sha256 |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for path in files():
        relative = path.relative_to(ROOT).as_posix()
        purpose, source = PURPOSES.get(relative, ("derived public evidence", "derived"))
        rows.append(f"| `{relative}` | {purpose} | {source} | {len(canonical_bytes(path))} | `{sha256(path)}` |")
    return "\n".join(rows) + "\n"


def check_manifest() -> list[str]:
    problems: list[str] = []
    if not MANIFEST.is_file():
        return ["manifest_missing"]
    expected = manifest_text()
    actual = MANIFEST.read_text(encoding="utf-8")
    if expected != actual:
        problems.append("manifest_out_of_date")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="write MANIFEST.md")
    parser.add_argument("--check", action="store_true", help="check package and manifest without writing")
    args = parser.parse_args()
    problems = scan()
    if args.write:
        MANIFEST.write_text(manifest_text(), encoding="utf-8")
    else:
        problems.extend(check_manifest())
    print({"files": len(files()), "canonical_utf8_lf_bytes": sum(len(canonical_bytes(path)) for path in files()), "problems": problems})
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
