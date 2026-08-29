"""OpenAlex public fallback backend.

OpenAlex offers a free/public REST API with polite rate limits.
No API key is required for basic access; an email-based polite pool
is available via the OPENALEX_EMAIL env var.

If the public endpoint requires a key in the future, this backend
will switch to adapter_only mode.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from optomind_research.provider_key_router import ProviderKeyRouter

OPENALEX_API_BASE = "https://api.openalex.org/works"
RATE_LIMIT_SECONDS = 1.0
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OPENALEX_KEYS_FILE = PROJECT_ROOT / "api_keys" / "openalex.txt"
LEGACY_OPENALEX_KEYS_FILE = Path.home() / "Desktop" / "openalex.txt"


def _openalex_email() -> Optional[str]:
    return os.environ.get("OPENALEX_EMAIL") or os.environ.get("CONTACT_EMAIL") or os.environ.get("UNPAYWALL_EMAIL") or None


def _split_keys(raw: str) -> List[str]:
    keys: List[str] = []
    for part in raw.replace(",", "\n").replace(";", "\n").splitlines():
        key = part.strip()
        if key:
            keys.append(key)
    return keys


def _openalex_api_keys() -> List[str]:
    """Load OpenAlex API keys without ever logging the secret values."""
    keys: List[str] = []
    for env_name in ("OPENALEX_API_KEYS", "OPENALEX_API_KEY"):
        raw = os.environ.get(env_name) or ""
        keys.extend(_split_keys(raw))
    configured = os.environ.get("OPENALEX_API_KEYS_FILE")
    key_files = [Path(configured)] if configured else [DEFAULT_OPENALEX_KEYS_FILE, LEGACY_OPENALEX_KEYS_FILE]
    for key_file in key_files:
        if key_file.exists():
            try:
                keys.extend(_split_keys(key_file.read_text(encoding="utf-8", errors="replace")))
            except Exception:
                pass
    seen: set[str] = set()
    unique: List[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


class OpenAlexBackend:
    """Search OpenAlex for works. Falls back gracefully if unavailable.

    Priority: if public query works → enabled backend.
    If public query fails or requires key → adapter_only.
    """

    def __init__(
        self,
        rate_limit: float | None = None,
        *,
        router: ProviderKeyRouter | None = None,
        opener: Any | None = None,
        sleep_fn: Any | None = None,
    ) -> None:
        self._last_request = 0.0
        self._rate_limit = rate_limit if rate_limit is not None else RATE_LIMIT_SECONDS
        self.enabled = True
        self.adapter_only = False
        self.last_error = ""
        self.stats: Dict[str, int] = {"requests": 0, "errors": 0, "rate_limited": 0, "keys_loaded": 0, "keys_exhausted": 0}
        self._opener = opener or urllib.request.urlopen
        api_keys = _openalex_api_keys()
        self.stats["keys_loaded"] = len(api_keys)
        self._router = router or ProviderKeyRouter(
            provider="openalex",
            keys=api_keys,
            min_interval_seconds=max(
                float(self._rate_limit or RATE_LIMIT_SECONDS),
                RATE_LIMIT_SECONDS,
            ),
            max_attempts=len(api_keys) or 1,
            sleep_fn=sleep_fn or time.sleep,
        )
        if not hasattr(OpenAlexBackend, "_global_lock"):
            OpenAlexBackend._global_lock = threading.Lock()
            OpenAlexBackend._global_last_request = 0.0
            OpenAlexBackend._global_disabled_until = 0.0
            OpenAlexBackend._global_last_error = ""

    def _respect_rate_limit(self) -> None:
        with OpenAlexBackend._global_lock:
            now = time.monotonic()
            elapsed = now - float(getattr(OpenAlexBackend, "_global_last_request", 0.0))
            delay = max(float(self._rate_limit or RATE_LIMIT_SECONDS), RATE_LIMIT_SECONDS) - elapsed
            if delay > 0:
                time.sleep(delay)
            OpenAlexBackend._global_last_request = time.monotonic()
            self._last_request = OpenAlexBackend._global_last_request

    def search(
        self,
        query: str,
        max_results: int = 10,
        from_year: int | None = None,
        sort: str = "relevance_score:desc",
    ) -> List[Dict[str, Any]]:
        """Search OpenAlex and return normalized SourceRecord dicts."""
        if self.adapter_only:
            return []
        api_keys = self._router.keys
        disabled_until = float(getattr(OpenAlexBackend, "_global_disabled_until", 0.0))
        if not api_keys and disabled_until and time.time() < disabled_until:
            self.last_error = str(getattr(OpenAlexBackend, "_global_last_error", "openalex temporarily disabled"))
            self.adapter_only = True
            return []

        base_params: Dict[str, Any] = {
            "search": query,
            "per_page": str(min(max_results, 50)),
            "sort": sort or "relevance_score:desc",
        }
        if from_year and int(from_year) > 0:
            base_params["filter"] = (
                f"from_publication_date:{int(from_year)}-01-01"
            )
        url = OPENALEX_API_BASE + "?" + urllib.parse.urlencode(base_params)
        data = self._request_with_router(url)
        if data is None:
            self.adapter_only = True
            return []

        results: List[Dict[str, Any]] = []
        for item in data.get("results", []):
            source = self._parse_item(item, query)
            if source:
                results.append(normalize_openalex_result(source))
        return results[:max_results]

    def get_work(self, work_id_or_doi: str) -> Optional[Dict[str, Any]]:
        """Fetch one OpenAlex work by OpenAlex ID (W...) or DOI.

        DOI lookups are encoded as the documented ``doi:<doi>`` path form.
        """
        identifier = str(work_id_or_doi or "").strip()
        if not identifier:
            return None
        if identifier.startswith("https://openalex.org/"):
            identifier = identifier.rsplit("/", 1)[-1]
        elif identifier.lower().startswith("https://doi.org/"):
            identifier = "doi:" + identifier.split("doi.org/", 1)[-1]
        elif identifier.lower().startswith("doi:"):
            identifier = "doi:" + identifier.split(":", 1)[-1]
        elif identifier.lower().startswith("10."):
            identifier = "doi:" + identifier
        url = OPENALEX_API_BASE + "/" + urllib.parse.quote(identifier, safe=":")
        data = self._request_with_router(url)
        if not data:
            return None
        source = self._parse_item(data, f"openalex_work:{work_id_or_doi}")
        if not source:
            return None
        source["raw_metadata"]["referenced_works"] = data.get("referenced_works", []) or []
        return normalize_openalex_result(source)

    def get_references(self, work_id_or_doi: str, max_results: int = 20) -> List[Dict[str, Any]]:
        """Fetch referenced works for a seed work, then hydrate them one by one."""
        seed = self.get_work(work_id_or_doi)
        if not seed:
            return []
        raw = seed.get("raw_metadata", {}) or {}
        referenced = raw.get("referenced_works", []) or []
        out: List[Dict[str, Any]] = []
        for ref in referenced[: max_results * 2]:
            ref_id = str(ref or "").rstrip("/").rsplit("/", 1)[-1]
            if not ref_id:
                continue
            work = self.get_work(ref_id)
            if work:
                work["backend"] = "openalex_reference"
                work["retrieval_method"] = "openalex_reference_chase"
                out.append(work)
            if len(out) >= max_results:
                break
        return out

    def _request_with_router(self, url: str) -> Optional[Dict[str, Any]]:
        """Fetch one URL through router-selected per-key lanes."""

        data: Optional[Dict[str, Any]] = None
        # One logical request tries each configured lane at most once; a
        # no-key public request is a single anonymous attempt.
        for _ in range(len(self._router.lanes)):
            lane, _ = self._router.acquire_lane()
            if lane is None:
                break
            data = self._request_json(url, lane)
            if data is not None:
                break
        return data

    def _request_json(
        self,
        url: str,
        lane: Any,
    ) -> Optional[Dict[str, Any]]:
        """One lane-scoped request; errors are classified internally."""

        if not lane.key:
            # Public/no-key path stays globally conservative across backend
            # instances; authenticated lanes pace per key in the router.
            self._respect_rate_limit()
        self.stats["requests"] += 1
        auth_params: Dict[str, str] = {}
        if lane.key:
            auth_params["api_key"] = lane.key
        else:
            email = _openalex_email()
            if email:
                auth_params["mailto"] = email
        final_url = url
        if auth_params:
            separator = "&" if "?" in url else "?"
            final_url = url + separator + urllib.parse.urlencode(auth_params)
        headers = {
            "User-Agent": "OptoMind/0.1 (mailto:anonymous@example.com)",
        }
        req = urllib.request.Request(final_url, headers=headers)
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
                    retry_after = exc.headers.get("Retry-After") if exc.headers else ""
                except Exception:
                    retry_after = ""
                try:
                    wait_s = min(20.0, max(3.0, float(retry_after or 5.0)))
                except Exception:
                    wait_s = 5.0
                self._router.rate_limit_cool_lane(lane, wait_s)
                if lane.key and wait_s >= 20.0:
                    self.stats["rate_limited"] += 1
                    self.stats["keys_exhausted"] += 1
                    self.last_error = (
                        f"HTTP 429 budget/rate limit (slot={slot})"
                    )
                else:
                    self.last_error = f"HTTP 429 rate limit (slot={slot})"
                if not lane.key:
                    try:
                        disabled_for = min(
                            24 * 3600.0,
                            max(600.0, float(retry_after or 3600.0)),
                        )
                    except Exception:
                        disabled_for = 3600.0
                    OpenAlexBackend._global_disabled_until = (
                        time.time() + disabled_for
                    )
                    OpenAlexBackend._global_last_error = self.last_error
                return None
            if code in {401, 403}:
                self._router.quarantine_lane(lane)
                self.last_error = (
                    f"HTTP {code} authentication failure (slot={slot})"
                )
                return None
            self.last_error = (
                f"HTTP {code}: {getattr(exc, 'reason', '')}"
            )
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

    def _parse_item(
        self, item: Dict[str, Any], query: str
    ) -> Optional[Dict[str, Any]]:
        try:
            title = item.get("title", "")
            openalex_id = item.get("id", "").split("/")[-1] if item.get("id") else ""

            authors: List[str] = []
            for authorship in item.get("authorships", []):
                author_info = authorship.get("author", {})
                name = author_info.get("display_name", "")
                if name:
                    authors.append(name)

            year = item.get("publication_year")

            doi = item.get("doi", "").replace("https://doi.org/", "") if item.get("doi") else ""

            abstract = ""
            abstract_inverted_index = item.get("abstract_inverted_index")
            if abstract_inverted_index and isinstance(abstract_inverted_index, dict):
                positioned_words = []
                for word, positions in abstract_inverted_index.items():
                    for position in positions or []:
                        positioned_words.append((int(position), word))
                positioned_words.sort(key=lambda pair: pair[0])
                abstract = " ".join(word for _, word in positioned_words)

            has_abstract = bool(abstract and len(abstract) > 50)

            primary_location = item.get("primary_location", {}) or {}
            source_info = primary_location.get("source", {}) or {}
            journal = source_info.get("display_name", "")
            best_oa = item.get("best_oa_location", {}) or {}
            open_access = item.get("open_access", {}) or {}
            cited_by_count = int(item.get("cited_by_count") or 0)
            source_url = (
                primary_location.get("landing_page_url")
                or item.get("id", "")
            )
            pdf_url = (
                best_oa.get("pdf_url")
                or primary_location.get("pdf_url")
                or ""
            )

            return {
                "source_id": f"openalex:{openalex_id}" if openalex_id else f"openalex:{title[:80]}",
                "title": title,
                "authors": authors,
                "year": year,
                "doi": doi,
                "url_or_doi": f"https://doi.org/{doi}" if doi else "",
                "openalex_id": openalex_id,
                "source_url": source_url,
                "pdf_url": pdf_url,
                "journal_or_venue": journal,
                "citation_count": cited_by_count,
                "cited_by_count": cited_by_count,
                "is_oa": bool(open_access.get("is_oa", False)),
                "oa_status": open_access.get("oa_status", ""),
                "open_access_url": open_access.get("oa_url", ""),
                "content_urls": item.get("content_urls", {}) or {},
                "has_content": item.get("has_content", {}) or {},
                "publisher": "",
                "abstract_or_snippet": abstract,
                "query": query,
                "retrieval_method": "openalex_api",
                "backend": "openalex",
                "verification_status": (
                    "verified" if doi else "verified_url"
                ),
                "evidence_extraction_ready": has_abstract,
                "relevance_score": 0.5,
                "raw_metadata": {
                    "type": item.get("type", ""),
                    "cited_by_count": cited_by_count,
                    "is_oa": bool(open_access.get("is_oa", False)),
                    "oa_status": open_access.get("oa_status", ""),
                    "has_content": item.get("has_content", {}) or {},
                    "content_urls": item.get("content_urls", {}) or {},
                },
            }
        except Exception:
            return None


def normalize_openalex_result(source: Dict[str, Any]) -> Dict[str, Any]:
    defaults = {
        "source_id": "", "title": "", "authors": [], "year": None,
        "doi": "", "url_or_doi": "", "arxiv_id": "",
        "semantic_scholar_paper_id": "", "openalex_id": "",
        "source_url": "", "pdf_url": "", "journal_or_venue": "",
        "publisher": "", "abstract_or_snippet": "", "query": "",
        "query_id": "", "retrieval_method": "", "backend": "openalex",
        "verification_status": "unverified", "evidence_extraction_ready": False,
        "relevance_score": 0.0, "backend_score": 0.0,
        "source_quality_score": 0.0, "raw_metadata": {},
        "local_pdf_path": "", "local_text_path": "",
        "local_chunks_path": "", "notes": "",
        "citation_count": 0, "cited_by_count": 0,
        "is_oa": None, "oa_status": "",
        "open_access_url": "", "content_urls": {}, "has_content": {},
    }
    for k, v in defaults.items():
        source.setdefault(k, v)
    return source
