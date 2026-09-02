"""Minimal credential and network probes for the local Review portal.

The script never prints a credential, response body, machine path, or provider
request identifier.  It returns one compact JSON object for the parent process.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


QWEN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
S2_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
OPENALEX_URL = "https://api.openalex.org/works"


def _values(path: Path, *, limit: int = 8) -> list[str]:
    if not path.is_file():
        return []
    values: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        value = raw.strip()
        if value and not value.startswith("#") and value not in values:
            values.append(value)
        if len(values) >= limit:
            break
    return values


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"服务返回 HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return "无法建立网络连接"
    if isinstance(exc, TimeoutError):
        return "连接超时"
    return type(exc).__name__


def _qwen_probe(path: Path) -> dict[str, str]:
    keys = _values(path)
    if not keys:
        return {"id": "qwen", "status": "failed", "detail": "Qwen 密钥文件为空。"}
    body = json.dumps(
        {
            "model": "qwen-flash",
            "messages": [{"role": "user", "content": "Reply only: OK"}],
            "max_tokens": 2,
            "temperature": 0,
        }
    ).encode("utf-8")
    last_error = "鉴权未通过"
    for key in keys:
        request = urllib.request.Request(
            QWEN_URL,
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read(256_000).decode("utf-8"))
            if payload.get("choices"):
                return {
                    "id": "qwen",
                    "status": "passed",
                    "detail": "最小真实模型请求成功；密钥内容未输出。",
                }
            last_error = "模型服务未返回有效结果"
        except (OSError, TimeoutError, ValueError, urllib.error.URLError) as exc:
            last_error = _safe_error(exc)
    return {"id": "qwen", "status": "failed", "detail": last_error}


def _s2_probe(path: Path) -> dict[str, str]:
    keys = _values(path)
    candidates: list[str | None] = keys[:4] or [None]
    query = urllib.parse.urlencode(
        {"query": "integrated photonics", "limit": 1, "fields": "title,year"}
    )
    last_error = "文献服务未返回有效结果"
    for key in candidates:
        headers = {"User-Agent": "OptoMind-Review-Local-Preflight/1.0"}
        if key:
            headers["x-api-key"] = key
        request = urllib.request.Request(f"{S2_URL}?{query}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                payload = json.loads(response.read(256_000).decode("utf-8"))
            if isinstance(payload.get("data"), list):
                mode = "带密钥访问" if key else "公共访问"
                return {
                    "id": "literature",
                    "status": "passed",
                    "detail": f"Semantic Scholar {mode}成功。",
                }
        except (OSError, TimeoutError, ValueError, urllib.error.URLError) as exc:
            last_error = _safe_error(exc)
    # The production harness is multi-source.  S2 throttling is not a hard
    # failure when the public OpenAlex fallback is reachable.
    fallback_query = urllib.parse.urlencode(
        {"search": "integrated photonics", "per-page": 1, "select": "id,title"}
    )
    request = urllib.request.Request(
        f"{OPENALEX_URL}?{fallback_query}",
        headers={"User-Agent": "OptoMind-Review-Local-Preflight/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            payload = json.loads(response.read(256_000).decode("utf-8"))
        if isinstance(payload.get("results"), list):
            return {
                "id": "literature",
                "status": "passed",
                "detail": f"Semantic Scholar 暂不可用（{last_error}）；OpenAlex 后备检索成功。",
            }
    except (OSError, TimeoutError, ValueError, urllib.error.URLError) as exc:
        last_error = f"S2：{last_error}；OpenAlex：{_safe_error(exc)}"
    return {"id": "literature", "status": "failed", "detail": last_error}


def main() -> int:
    qwen_path = Path(os.environ.get("QWEN_API_KEY_FILE", ""))
    s2_path = Path(os.environ.get("SEMANTIC_SCHOLAR_API_KEYS_FILE", ""))
    checks = [_qwen_probe(qwen_path), _s2_probe(s2_path)]
    ready = all(row["status"] == "passed" for row in checks)
    print(json.dumps({"ready": ready, "checks": checks}, ensure_ascii=False))
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
