"""Strict UTF-8 splitter and verifier for the evidence BODY archive.

Splitting is performed on Unicode character boundaries. Each part is decoded
strictly and the manifest records canonical byte length and SHA-256. The
verifier reconstructs every source before accepting the archive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def split_utf8(source: Path, destination: Path, max_bytes: int = 600_000) -> Path:
    raw = source.read_bytes()
    text = raw.decode("utf-8")
    if text.encode("utf-8") != raw:
        raise ValueError(f"non-canonical UTF-8: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    parts = []
    chars: list[str] = []
    size = 0
    index = 1

    def flush() -> None:
        nonlocal chars, size, index
        if not chars:
            return
        payload = "".join(chars).encode("utf-8")
        path = destination / f"{source.name}.part-{index:04d}"
        path.write_bytes(payload)
        parts.append({"path": path.name, "bytes": len(payload), "sha256": digest(payload)})
        chars = []
        size = 0
        index += 1

    for char in text:
        encoded = char.encode("utf-8")
        if chars and size + len(encoded) > max_bytes:
            flush()
        chars.append(char)
        size += len(encoded)
    flush()
    manifest = {
        "type": "strict_utf8_split_manifest",
        "source_name": source.name,
        "source_bytes": len(raw),
        "source_sha256": digest(raw),
        "encoding": "utf-8",
        "parts": parts,
    }
    manifest_path = destination / f"{source.name}.parts.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def verify_manifest(manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload = bytearray()
    for item in manifest["parts"]:
        part = manifest_path.parent / item["path"]
        data = part.read_bytes()
        data.decode("utf-8")
        if len(data) != item["bytes"] or digest(data) != item["sha256"]:
            raise ValueError(f"part mismatch: {part}")
        payload.extend(data)
    rebuilt = bytes(payload)
    if len(rebuilt) != manifest["source_bytes"] or digest(rebuilt) != manifest["source_sha256"]:
        raise ValueError(f"reassembled source mismatch: {manifest_path}")
    rebuilt.decode("utf-8")
    return {"manifest": str(manifest_path), "parts": len(manifest["parts"]), "bytes": len(rebuilt), "sha256": digest(rebuilt)}


def build_index(root: Path) -> dict:
    files = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.name != "PUBLIC_FILE_INDEX.json"):
        data = path.read_bytes()
        data.decode("utf-8")
        files.append({"path": path.relative_to(root).as_posix(), "bytes": len(data), "sha256": digest(data)})
    index = {"archive": root.name, "encoding": "utf-8", "files": files}
    (root / "PUBLIC_FILE_INDEX.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return index


def verify(root: Path) -> dict:
    index = json.loads((root / "PUBLIC_FILE_INDEX.json").read_text(encoding="utf-8"))
    for item in index["files"]:
        path = root / item["path"]
        data = path.read_bytes()
        data.decode("utf-8")
        if len(data) != item["bytes"] or digest(data) != item["sha256"]:
            raise ValueError(f"indexed file mismatch: {path}")
    reports = [verify_manifest(path) for path in root.rglob("*.parts.json")]
    return {"indexed_files_verified": len(index["files"]), "split_manifests_verified": len(reports), "reports": reports}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--split", type=Path, help="split one UTF-8 source into this directory")
    parser.add_argument("--max-bytes", type=int, default=600_000)
    parser.add_argument("--index", action="store_true")
    args = parser.parse_args()
    if args.split:
        print(json.dumps({"manifest": str(split_utf8(args.split, args.root, args.max_bytes))}, ensure_ascii=False))
    if args.index:
        build_index(args.root)
    print(json.dumps(verify(args.root), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
