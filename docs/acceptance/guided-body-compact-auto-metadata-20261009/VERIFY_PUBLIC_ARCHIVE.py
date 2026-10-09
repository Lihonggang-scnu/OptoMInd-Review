from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path


BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
API_RX = re.compile(r"(?i)\b(?:sk|key|api)[-_][A-Za-z0-9]{16,}\b")
SIGNED_URL = re.compile(r"(?i)https?://[^\s<>\"']+[?&][^\s<>\"']*(?:sig(?:nature)?|token|api[_-]?key|expires|auth)[^\s<>\"']*")
AUTH_VALUE = re.compile(
    r"(?i)(?:" + "|".join([
        "author" + "ization", "api" + "[_-]?key", "access" + "[_-]?token",
        "refresh" + "[_-]?token", "pass" + "word", "cookie",
        "private" + "[_-]?key", "key" + "[_-]?file",
    ]) + r")\s*[:=]\s*(?![\"']?\[REDACTED)[^\s,;)}\]]{8,}"
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    rebuilt = bytearray()
    for item in manifest["parts"]:
        part = path.parent / item["path"]
        data = part.read_bytes()
        data.decode("utf-8")
        if len(data) != item["bytes"] or sha(data) != item["sha256"]:
            raise ValueError(f"part mismatch: {part}")
        rebuilt.extend(data)
    payload = bytes(rebuilt)
    if len(payload) != manifest["source_bytes"] or sha(payload) != manifest["source_sha256"]:
        raise ValueError(f"reassembled source mismatch: {path}")
    return {"path": path.relative_to(path.parents[2]).as_posix(), "parts": len(manifest["parts"]), "bytes": len(payload), "sha256": sha(payload)}


def verify(root: Path) -> dict:
    index = json.loads((root / "PUBLIC_FILE_INDEX.json").read_text(encoding="utf-8"))
    errors = []
    secret_hits = []
    json_errors = []
    forbidden_paths = []
    for item in index["files"]:
        path = (root / item["path"]).resolve()
        if root.resolve() not in path.parents:
            errors.append({"path": item["path"], "error": "path escapes archive"})
            continue
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            errors.append({"path": item["path"], "error": str(exc)})
            continue
        if len(data) != item["bytes"] or sha(data) != item["sha256"]:
            errors.append({"path": item["path"], "error": "index mismatch"})
        rel = path.relative_to(root).as_posix()
        if path.name.endswith(".sse.raw") or path.name.endswith(".sse.partial") or path.suffix.lower() in {".sqlite", ".db"}:
            forbidden_paths.append(rel)
        for name, rx in (("bearer", BEARER), ("api_key", API_RX), ("signed_url", SIGNED_URL), ("auth_value", AUTH_VALUE)):
            if rx.search(text):
                secret_hits.append({"path": rel, "pattern": name})
        if path.suffix.lower() == ".json" and not path.name.endswith(".parts.json"):
            try:
                json.loads(text)
            except Exception as exc:
                json_errors.append({"path": rel, "error": str(exc)})
    reports = []
    for manifest in root.rglob("*.parts.json"):
        reports.append(verify_manifest(manifest))
    return {"indexed_files_verified": len(index["files"]), "split_manifests_verified": len(reports), "index_or_utf8_errors": errors, "json_errors": json_errors, "sensitive_hits": secret_hits, "forbidden_paths": forbidden_paths, "max_indexed_bytes": max((x["bytes"] for x in index["files"]), default=0)}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: VERIFY_PUBLIC_ARCHIVE.py ARCHIVE_ROOT")
    result = verify(Path(sys.argv[1]).resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(1 if any(result[key] for key in ("index_or_utf8_errors", "json_errors", "sensitive_hits", "forbidden_paths")) else 0)
