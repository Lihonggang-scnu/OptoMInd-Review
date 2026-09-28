"""Small bounded Firecrawl reader used by local material rescue.

The existing research search engine is intentionally not used here: its
general search cache and fallback routes are useful for research discovery but
are too broad for an auditable per-paper rescue.  This adapter makes one exact
Firecrawl v1 request per logical operation, accepts an explicit deadline, and
keeps transport status separate from an empty result.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

from ...config import load_secret_candidates


class FirecrawlReader:
    """Bounded Firecrawl v1 search/scrape adapter with no fallback provider."""

    def __init__(
        self,
        *,
        keys: list[str] | None = None,
        timeout_seconds: float = 20.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.keys = list(keys if keys is not None else load_secret_candidates("FIRECRAWL_API_KEY"))
        self.timeout_seconds = max(0.5, float(timeout_seconds))
        self._opener = opener or urllib.request.urlopen
        self._state = threading.local()
        self.last_error = ""
        self.last_status = "ready"
        self.calls: list[dict[str, Any]] = []
        self._request_lock = threading.Lock()
        self._next_request_at = 0.0

    @property
    def last_error(self) -> str:
        return str(getattr(self._state, "last_error", ""))

    @last_error.setter
    def last_error(self, value: Any) -> None:
        self._state.last_error = str(value or "")

    @property
    def last_status(self) -> str:
        return str(getattr(self._state, "last_status", "ready"))

    @last_status.setter
    def last_status(self, value: Any) -> None:
        self._state.last_status = str(value or "ready")

    def _request(self, endpoint: str, payload: Mapping[str, Any], *, deadline: float | None = None) -> Mapping[str, Any] | None:
        wait_budget = max(0.0, deadline - time.monotonic()) if deadline is not None else self.timeout_seconds
        if not self._request_lock.acquire(timeout=wait_budget):
            self.last_error, self.last_status = "reader_queue_deadline", "provider_error"
            return None
        try:
            pause = max(0.0, self._next_request_at - time.monotonic())
            if deadline is not None and pause >= deadline - time.monotonic():
                self.last_error, self.last_status = "reader_cooldown_deadline", "provider_error"
                return None
            if pause:
                time.sleep(pause)
            result = self._request_once(endpoint, payload, deadline=deadline)
            self._next_request_at = max(self._next_request_at, time.monotonic() + 1.0)
            return result
        finally:
            self._request_lock.release()

    def _request_once(self, endpoint: str, payload: Mapping[str, Any], *, deadline: float | None = None) -> Mapping[str, Any] | None:
        if not self.keys:
            self.last_error = "firecrawl_key_missing"
            self.last_status = "provider_error"
            return None
        remaining = self.timeout_seconds if deadline is None else min(self.timeout_seconds, deadline - time.monotonic())
        if deadline is not None and time.monotonic() >= deadline:
            self.last_error = "deadline"
            self.last_status = "provider_error"
            return None
        key = self.keys[0]
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "OptoMind-Local-Materials/1.0",
            },
        )
        call = {"endpoint": endpoint, "status": "started"}
        try:
            with self._opener(request, timeout=remaining) as response:
                raw = response.read()
                status = int(getattr(response, "status", 200) or 200)
            call.update({"status": "ok" if 200 <= status < 300 else "provider_error", "http_status": status, "bytes": len(raw)})
            if not 200 <= status < 300:
                self.last_error = f"HTTP {status}"
                self.last_status = "provider_error"
                return None
            payload_out = json.loads(raw.decode("utf-8", errors="replace"))
            if not isinstance(payload_out, Mapping):
                raise ValueError("firecrawl_response_not_object")
            # Firecrawl can return HTTP 200 for quota, auth, or validation
            # failures.  Keep those distinct from an empty successful search
            # or scrape and retain only a stable, non-sensitive reason.
            if payload_out.get("success") is False:
                call.update({"status": "provider_error", "semantic_error": "firecrawl_api_error"})
                self.last_error = "firecrawl_api_error"
                self.last_status = "provider_error"
                return None
            self.last_error = ""
            self.last_status = "ok"
            return payload_out
        except urllib.error.HTTPError as exc:
            code = int(getattr(exc, "code", 0) or 0)
            call.update({"status": "provider_error", "http_status": code})
            if code == 429:
                try:
                    retry_after = max(1.0, float(exc.headers.get("Retry-After", "15")))
                except (TypeError, ValueError, AttributeError):
                    retry_after = 15.0
                self._next_request_at = time.monotonic() + retry_after
                call["retry_after_seconds"] = retry_after
            self.last_error = f"HTTP {code}"
            self.last_status = "provider_error"
            return None
        except Exception as exc:
            call.update({"status": "provider_error", "error": type(exc).__name__})
            self.last_error = type(exc).__name__
            self.last_status = "provider_error"
            return None
        finally:
            self.calls.append(call)

    def search(self, query: str, *, max_results: int = 8, deadline: float | None = None) -> list[dict[str, Any]]:
        payload = self._request(
            "https://api.firecrawl.dev/v1/search",
            {"query": str(query), "limit": min(20, max(1, int(max_results)))},
            deadline=deadline,
        )
        if payload is None:
            return []
        rows = payload.get("data") if isinstance(payload, Mapping) else []
        if isinstance(rows, Mapping):
            rows = rows.get("web") or rows.get("results") or []
        output: list[dict[str, Any]] = []
        for rank, row in enumerate(rows or []):
            if not isinstance(row, Mapping):
                continue
            url = str(row.get("url") or row.get("source_url") or "").strip()
            if not url:
                continue
            output.append({
                "title": str(row.get("title") or ""),
                "source_url": url,
                "url_or_doi": url,
                "abstract_or_snippet": str(row.get("description") or row.get("content") or "")[:1000],
                "backend": "firecrawl",
                "search_rank": rank,
                "verification_status": "unverified",
            })
        return output[: max(1, int(max_results))]

    def fetch_fulltext(self, url: str, *, deadline: float | None = None, method: str = "firecrawl") -> str:
        if str(method or "firecrawl").casefold() != "firecrawl":
            self.last_error = "unsupported_reader_method"
            self.last_status = "provider_error"
            return ""
        payload = self._request(
            "https://api.firecrawl.dev/v1/scrape",
            {"url": str(url), "formats": ["markdown"]},
            deadline=deadline,
        )
        if payload is None:
            return ""
        data = payload.get("data") if isinstance(payload, Mapping) else {}
        if not isinstance(data, Mapping):
            data = payload
        markdown = data.get("markdown") if isinstance(data, Mapping) else ""
        return str(markdown or "")


__all__ = ["FirecrawlReader"]
