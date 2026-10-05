"""Build the publication-stage manifest and scan staged bytes for exposed secrets."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "MANIFEST.sha256.json"
SAFETY_SCAN = ROOT / "SAFETY_SCAN.json"

PATTERNS = {
    "credential_like": [
        re.compile(rb"\bsk-[A-Za-z0-9_-]{16,}\b"),
        re.compile(rb"(?:api[_-]?key|access[_-]?key|secret[_-]?key)\s*[:=]\s*[^\s\"'`]{8,}", re.I),
        re.compile(rb"authorization\s*:\s*bearer\s+[^\s\"']{8,}", re.I),
        re.compile(rb"-----BEGIN (?:RSA|OPENSSH|EC|DSA) PRIVATE KEY-----"),
        re.compile(rb"(?:aws_access_key_id|aws_secret_access_key)\s*[:=]\s*[^\s\"']{8,}", re.I),
    ],
    "signed_url_like": [
        re.compile(rb"(?:x-amz-signature|x-amz-credential|x-amz-security-token)\s*=\s*[^&\s]{8,}", re.I),
        re.compile(rb"[?&](?:sig|signature|se|sp|sv)=[^&\s]{8,}", re.I),
    ],
}


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def staged_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file() and path not in {MANIFEST, SAFETY_SCAN}
    )


def main() -> None:
    files = staged_files()
    matches: dict[str, list[dict[str, object]]] = {name: [] for name in PATTERNS}
    for path in files:
        data = path.read_bytes()
        relative = path.relative_to(ROOT).as_posix()
        for category, patterns in PATTERNS.items():
            count = sum(len(pattern.findall(data)) for pattern in patterns)
            if count:
                matches[category].append({"path": relative, "match_count": count})

    scan = {
        "scope": ".",
        "files_scanned": len(files),
        "bytes_scanned": sum(path.stat().st_size for path in files),
        "metadata_files_excluded_from_scan": ["MANIFEST.sha256.json", "SAFETY_SCAN.json"],
        "credential_like_match_count": sum(item["match_count"] for item in matches["credential_like"]),
        "signed_url_like_match_count": sum(item["match_count"] for item in matches["signed_url_like"]),
        "matches": matches,
    }
    SAFETY_SCAN.write_text(json.dumps(scan, indent=2) + "\n", encoding="utf-8")

    manifest_files = sorted(
        path for path in ROOT.rglob("*") if path.is_file() and path != MANIFEST
    )
    entries = [
        {
            "path": path.relative_to(ROOT).as_posix(),
            "size": path.stat().st_size,
            "sha256": digest(path),
        }
        for path in manifest_files
    ]
    manifest = {
        "scope": ".",
        "manifest_excludes_self": True,
        "file_count": len(entries),
        "total_bytes": sum(entry["size"] for entry in entries),
        "files": entries,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        f"staged_files={len(entries)} staged_bytes={manifest['total_bytes']} "
        f"credential_like={scan['credential_like_match_count']} "
        f"signed_url_like={scan['signed_url_like_match_count']}"
    )


if __name__ == "__main__":
    main()
