"""Verify canonical archive payloads after GitHub text-blob normalization.

The GitHub connector may append one CRLF trailer to a text blob. The public
index records the local canonical payload bytes and SHA-256. This script
accepts either canonical bytes or exactly one trailing CRLF, strips only that
trailer, verifies every indexed hash, and verifies every split manifest after
reassembly. Pass --canonical-dir to write normalized copies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _json_without_optional_trailer(path: Path):
    raw = path.read_bytes()
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        if raw.endswith(b"\r\n"):
            return json.loads(raw[:-2].decode("utf-8"))
        raise


def _canonical(path: Path, expected_bytes: int, expected_sha: str) -> bytes:
    raw = path.read_bytes()
    if len(raw) == expected_bytes + 2 and raw.endswith(b"\r\n"):
        raw = raw[:-2]
    if len(raw) != expected_bytes:
        raise ValueError(f"byte length mismatch: {path} got {len(raw)} expected {expected_bytes}")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha:
        raise ValueError(f"SHA-256 mismatch: {path} got {digest} expected {expected_sha}")
    return raw


def verify(root: Path, canonical_dir: Path | None = None) -> dict:
    index_path = root / "PUBLIC_FILE_INDEX.json"
    index = _json_without_optional_trailer(index_path)
    entries = {item["path"]: item for item in index["files"]}
    normalized = 0
    for rel, item in entries.items():
        raw = _canonical(root / rel, int(item["bytes"]), item["sha256"])
        normalized += 1
        if canonical_dir is not None:
            out = canonical_dir / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(raw)

    manifests = 0
    reassembled = 0
    for rel, item in entries.items():
        if not rel.endswith(".parts.json"):
            continue
        manifest = json.loads(_canonical(root / rel, int(item["bytes"]), item["sha256"]).decode("utf-8"))
        parts = []
        for part in manifest["parts"]:
            part_item = entries.get(part["path"])
            if part_item is None:
                raise ValueError(f"manifest part missing from index: {part['path']}")
            payload = _canonical(root / part["path"], int(part_item["bytes"]), part_item["sha256"])
            if len(payload) != int(part["bytes"]) or hashlib.sha256(payload).hexdigest() != part["sha256"]:
                raise ValueError(f"manifest part mismatch: {part['path']}")
            parts.append(payload)
        payload = b"".join(parts)
        if len(payload) != int(manifest["source_bytes"]):
            raise ValueError(f"reassembled length mismatch: {manifest['source_path']}")
        digest = hashlib.sha256(payload).hexdigest()
        if digest != manifest["source_sha256"]:
            raise ValueError(f"reassembled SHA-256 mismatch: {manifest['source_path']}")
        manifests += 1
        if canonical_dir is not None:
            out = canonical_dir / "reassembled" / manifest["source_path"]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(payload)
        reassembled += 1
    return {"indexed_files_verified": normalized, "split_manifests_verified": manifests, "reassembled_sources": reassembled}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path, help="archive root containing PUBLIC_FILE_INDEX.json")
    parser.add_argument("--canonical-dir", type=Path, help="optional output directory for normalized copies")
    args = parser.parse_args()
    print(json.dumps(verify(args.root, args.canonical_dir), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

