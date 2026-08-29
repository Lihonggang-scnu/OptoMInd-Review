"""CORE API adapter - OA paper metadata and full-text access.

CORE API: https://core.ac.uk/services/api
Requires at least one CORE API key for full access.  Public fallback remains
disabled.  Multiple keys (env or project/legacy ``api_keys/core_api.txt``)
are routed through the provider-neutral lane router with per-key pacing,
429 progressive backoff, and 401/403 quarantine.  Credential values are
never logged or stored in error strings.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from optomind_research.provider_key_router import ProviderKeyRouter

CORE_API_BASE = "https://api.core.ac.uk/v3"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORE_KEYS_FILE = PROJECT_ROOT / "api_keys" / "core_api.txt"
LEGACY_CORE_KEYS_FILE = Path.home() / "Desktop" / "core_api.txt"


def _split_keys(raw: str) -> List[str]:
    keys: List[str] = []
    for part in raw.replace(",", "\n").replace(";", "\n").splitlines():
        key = part.strip()
        if key:
            keys.append(key)
    return keys


def _core_keys() -> List[str]:
    """Load one or more CORE API keys without ever logging secret values."""

    keys: List[str] = []
    for env_name in ("CORE_API_KEYS", "CORE_API_KEY"):
        raw = os.environ.get(env_name) or ""
        keys.extend(_split_keys(raw))
    configured = os.environ.get("CORE_API_KEYS_FILE")
    key_files = (
        [Path(configured)]
        if configured
        else [DEFAULT_CORE_KEYS_FILE, LEGACY_CORE_KEYS_FILE]
    )
    for key_file in key_files:
        if key_file.exists():
            try:
                keys.extend(
                    _split_keys(
                        key_file.read_text(encoding="utf-8", errors="replace")
                    )
                )
            except Exception:
                pass
    seen: set[str] = set()
    unique: List[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def _core_key() -> Optional[str]:
    """Compatibility helper returning the first configured CORE key."""

    keys = _core_keys()
    return keys[0] if keys else None


class CoreBackend:
    """Search CORE for OA papers. Disabled if no API key."""

    def __init__(
        self,
        *,
        router: ProviderKeyRouter | None = None,
        opener: Any | None = None,
        sleep_fn: Any | None = None,
    ) -> None:
        self._api_keys = _core_keys()
        self.enabled = bool(self._api_keys)
        self._has_key = self.enabled
        self.last_error = ""
        self.stats: Dict[str, int] = {
            "requests": 0,
            "errors": 0,
            "keys_loaded": len(self._api_keys),
        }
        self._opener = opener or urllib.request.urlopen
        self._router = router or ProviderKeyRouter(
            provider="core",
            keys=self._api_keys,
            min_interval_seconds=max(
                0.0,
                float(os.environ.get("CORE_MIN_INTERVAL_SEC", "1.0")),
            ),
            max_attempts=len(self._api_keys) or 1,
            sleep_fn=sleep_fn or time.sleep,
        )

    def search(self, query: str, max_results: int = 10) -> List[Dict[str, Any]]:
        """Search CORE. Returns empty list if no key."""

        if not self._has_key:
            return []

        params = {"q": query, "limit": str(min(max_results, 100))}
        url = f"{CORE_API_BASE}/search/works?{urllib.parse.urlencode(params)}"
        data = self._fetch_json(url)
        if data is None:
            return []

        results = []
        for item in data.get("results", []):
            raw_download = item.get("downloadUrl", "")
            fulltext_urls = item.get("sourceFulltextUrls")
            first_fulltext = ""
            if isinstance(fulltext_urls, list) and fulltext_urls:
                candidate = fulltext_urls[0]
                if isinstance(candidate, str) and candidate:
                    first_fulltext = candidate
            results.append({
                "source_id": f"core:{item.get('id', '')}",
                "title": item.get("title", ""),
                "authors": [a.get("name", "") for a in item.get("authors", [])],
                "year": item.get("yearPublished"),
                "doi": item.get("doi", ""),
                "url_or_doi": raw_download or first_fulltext,
                "source_url": item.get("downloadUrl", ""),
                "abstract_or_snippet": item.get("abstract", ""),
                "backend": "core",
                "retrieval_method": "core_api",
                "verification_status": "verified" if item.get("doi") else "unverified",
                "evidence_extraction_ready": bool(item.get("abstract", "") and len(item.get("abstract", "")) > 50),
                "relevance_score": 0.5,
                "raw_metadata": {"publisher": item.get("publisher", ""), "language": item.get("language", {}).get("name", "")},
            })
        return results

    def _fetch_json(self, url: str) -> Optional[Dict[str, Any]]:
        """Fetch one URL through router-selected per-key lanes."""

        # One logical request tries each configured lane at most once.
        for _ in range(len(self._router.lanes)):
            lane, _ = self._router.acquire_lane()
            if lane is None:
                break
            data = self._request_json(url, lane)
            if data is not None:
                return data
        return None

    def _request_json(self, url: str, lane: Any) -> Optional[Dict[str, Any]]:
        """One lane-scoped CORE request with internal error classification."""

        self.stats["requests"] += 1
        headers = {"User-Agent": "OptoMind/0.1"}
        if lane.key:
            headers["Authorization"] = f"Bearer {lane.key}"
        req = urllib.request.Request(url, headers=headers)
        slot = self._router.local_slot(lane)
        try:
            with self._opener(req, timeout=30) as resp:
                self.last_error = ""
                self._router.reset_lane_penalty(lane)
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            code = int(getattr(exc, "code", 0))
            self.stats["errors"] += 1
            if code == 429:
                retry_after = ""
                try:
                    retry_after = (
                        exc.headers.get("Retry-After")
                        if exc.headers
                        else ""
                    )
                except Exception:
                    retry_after = ""
                try:
                    wait_s = min(30.0, max(1.0, float(retry_after or 5.0)))
                except Exception:
                    wait_s = 5.0
                self._router.rate_limit_cool_lane(lane, wait_s)
                self.last_error = f"HTTP 429 rate limit (slot={slot})"
                return None
            if code in {401, 403}:
                self._router.quarantine_lane(lane)
                self.last_error = (
                    f"HTTP {code} authentication failure (slot={slot})"
                )
                return None
            self.last_error = f"HTTP {code}: {getattr(exc, 'reason', '')}"
            if 500 <= code <= 599:
                self._router.cool_lane(lane, min(8.0, 5.0))
            return None
        except Exception as exc:
            self.stats["errors"] += 1
            self.last_error = f"{type(exc).__name__}"
            self._router.cool_lane(lane, 2.0)
            return None
        finally:
            self._router.release_lane(lane)

    def check_status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "has_api_key": self._has_key,
            "api_key_env": "CORE_API_KEY",
        }
