"""Replay the recorded format-only repair for the second Max response.

The replay is intentionally self-contained: it reads only this public archive,
reconstructs the original response from its byte parts, and inserts the eight
recorded backslashes into the decoded ``content`` string.  It does not import
OptoMind production code, open a ledger, read credentials, or call a model.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ARCHIVE = Path(__file__).resolve().parents[1]
RAW_INDEX = ARCHIVE / "run" / "responses" / "maker_002" / "RAW_RESPONSE.json.parts.json"
RECOVERY_RECORD = ARCHIVE / "recovery" / "FORMAT_RECOVERY_RECORD.json"
ESCAPING_ONLY = ARCHIVE / "recovery" / "MODEL_RESPONSE_ESCAPING_ONLY.json"
DRAFT_GUIDE = ARCHIVE / "guide" / "DRAFT_GUIDE.json"
RECOVERED_GUIDE = ARCHIVE / "guide" / "GUIDE_RECOVERED_FOR_REVIEW.json"
MAX_PART_BYTES = 180_000


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def reconstruct_from_index(index_path: Path) -> tuple[bytes, dict[str, Any]]:
    index = read_json(index_path)
    parts = index["parts"]
    if index["max_part_bytes"] != MAX_PART_BYTES:
        raise AssertionError("unexpected part-size policy")
    if index["part_count"] != len(parts):
        raise AssertionError("parts index count mismatch")
    chunks: list[bytes] = []
    expected_offset = 0
    for expected_index, part in enumerate(parts, start=1):
        if part["index"] != expected_index or part["offset"] != expected_offset:
            raise AssertionError("parts are not contiguous and ordered")
        path = ARCHIVE / part["path"]
        chunk = path.read_bytes()
        if len(chunk) != part["bytes"] or len(chunk) > MAX_PART_BYTES:
            raise AssertionError(f"part byte count mismatch: {path}")
        if sha256(chunk) != part["sha256"]:
            raise AssertionError(f"part hash mismatch: {path}")
        chunks.append(chunk)
        expected_offset += len(chunk)
    data = b"".join(chunks)
    if len(data) != index["source_bytes"] or sha256(data) != index["source_sha256"]:
        raise AssertionError("reconstructed original response does not match its parts index")
    return data, index


def main() -> None:
    raw, raw_index = reconstruct_from_index(RAW_INDEX)
    recovery = read_json(RECOVERY_RECORD)
    if recovery["original_response_sha256"] != raw_index["source_sha256"]:
        raise AssertionError("recovery record and raw-response index disagree")
    if recovery["original_run_status"] != "response_invalid":
        raise AssertionError("original response status was rewritten")
    edits = recovery["edits"]
    if recovery["inserted_backslashes"] != 8 or recovery["byte_delta"] != 8 or len(edits) != 8:
        raise AssertionError("recovery record does not contain exactly eight edits")
    offsets = [edit["original_offset"] for edit in edits]
    if offsets != sorted(offsets) or len(set(offsets)) != 8:
        raise AssertionError("recovery offsets are not unique and ordered")
    if any(edit["operation"] != "insert_backslash_before_inner_quote" for edit in edits):
        raise AssertionError("recovery contains an operation other than backslash insertion")

    original_envelope = json.loads(raw.decode("utf-8"))
    original_content = original_envelope["content"]
    if any(original_content[offset] != '"' for offset in offsets):
        raise AssertionError("a recorded offset does not point to the original inner quote")
    if any(offset and original_content[offset - 1] == "\\" for offset in offsets):
        raise AssertionError("a recorded quote was already escaped")

    repaired_chars = list(original_content)
    for offset in reversed(offsets):
        repaired_chars.insert(offset, "\\")
    repaired_content = "".join(repaired_chars)
    if len(repaired_content) != len(original_content) + 8:
        raise AssertionError("repair changed content by more than eight characters")
    repaired = json.loads(repaired_content)

    expected_response = read_json(ESCAPING_ONLY)
    if repaired != expected_response:
        raise AssertionError("repaired response does not equal MODEL_RESPONSE_ESCAPING_ONLY")
    draft = read_json(DRAFT_GUIDE)
    recovered_guide = read_json(RECOVERED_GUIDE)
    if repaired["guide"] != draft or repaired["guide"] != recovered_guide:
        raise AssertionError("repaired guide does not equal both preserved and recovered guides")

    print(
        json.dumps(
            {
                "status": "passed",
                "original_response_bytes": len(raw),
                "original_response_sha256": sha256(raw),
                "inserted_backslashes": 8,
                "repaired_response_equals_archived_replay": True,
                "guide_equals_draft_and_recovered": True,
                "model_calls": 0,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
