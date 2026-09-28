"""Unified search engine — academic + web search, like GPT-Researcher.

Backends (18 total, 8 functional now, 10 need API keys):
  FUNCTIONAL NOW (no key needed):
    arxiv, crossref, openalex, duckduckgo, github, unpaywall, local_import, semantic_scholar_public

  NEED API KEY (adapter ready):
    tavily (have key!), semantic_scholar_full, google, bing, brave, serper, serpapi,
    pubmed, core, searchapi

  FULL-TEXT FETCHERS (post-search):
    jina (have key!) — r.jina.ai for clean markdown extraction
    firecrawl (have key!) — firecrawl.dev for full-page scraping

Usage:
  engine = SearchEngine(tavily_key="tvly-...")
  results = engine.search("optical coating design", backends=["tavily","arxiv","duckduckgo"])
  fulltext = engine.fetch_fulltext("https://example.com/paper")  # post-search enrichment
"""

from __future__ import annotations

import json, os, re, sqlite3, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from .config import (
    configure_secret_environment,
    load_secret,
    load_secret_candidates,
)
from .provider_key_router import ProviderKeyLane, ProviderKeyRouter


def _load_tavily_key() -> Optional[str]:
    """Load Tavily API key from file or env."""
    return load_secret("TAVILY_API_KEY")


def _load_jina_key() -> Optional[str]:
    return load_secret("JINA_API_KEY")


def _load_firecrawl_key() -> Optional[str]:
    return load_secret("FIRECRAWL_API_KEY")


def _load_s2_key() -> Optional[str]:
    return load_secret("SEMANTIC_SCHOLAR_API_KEY")


def _dedupe_key_pool(values: List[Optional[str]]) -> List[str]:
    """Deduplicate configured key candidates without exposing their values."""

    seen: set[str] = set()
    unique: List[str] = []
    for value in values or []:
        key = str(value or "").strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def _load_jina_key_pool(legacy_key: Optional[str] = None) -> List[str]:
    """Load every Jina candidate from env/project/legacy files, deduplicated."""

    return _dedupe_key_pool(
        [legacy_key, *load_secret_candidates("JINA_API_KEY")]
    )


def _load_firecrawl_key_pool(legacy_key: Optional[str] = None) -> List[str]:
    """Load every Firecrawl candidate from env/project/legacy files."""

    return _dedupe_key_pool(
        [legacy_key, *load_secret_candidates("FIRECRAWL_API_KEY")]
    )


def _apply_lane_http_status(
    router: ProviderKeyRouter,
    lane: ProviderKeyLane,
    status_code: int,
) -> None:
    """Update only the selected lane from one provider HTTP status."""

    if status_code in (401, 403):
        router.quarantine_lane(lane)
    elif status_code == 429:
        router.rate_limit_cool_lane(lane, 30.0)
    elif status_code == 408 or status_code >= 500:
        router.cool_lane(lane, 5.0)
    else:
        router.reset_lane_penalty(lane)


def _router_has_usable_lane(router: ProviderKeyRouter) -> bool:
    """True when at least one lane can be acquired without waiting.

    Conservative read of public lane state: if every lane is cooled or
    quarantined, the logical request returns to the higher-level fallback
    instead of sleeping on cooldowns. Briefly busy lanes are not treated as
    exhausted; acquire_lane will wait for them to release.
    """

    now = router.now_fn()
    return any(
        not lane.quarantined and now >= lane.cool_until
        for lane in router.lanes
    )


class SearchEngine:
    """Unified multi-backend search like GPT-Researcher."""

    def __init__(self, tavily_key: str | None = None, jina_key: str | None = None, firecrawl_key: str | None = None, s2_key: str | None = None):
        configure_secret_environment()
        self.tavily_key = tavily_key or _load_tavily_key()
        self.jina_key = jina_key or _load_jina_key()
        self.firecrawl_key = firecrawl_key or _load_firecrawl_key()
        self.s2_key = s2_key or _load_s2_key()
        self._jina_keys = _load_jina_key_pool(self.jina_key)
        self._firecrawl_keys = _load_firecrawl_key_pool(self.firecrawl_key)
        self._jina_router: Optional[ProviderKeyRouter] = None
        self._firecrawl_router: Optional[ProviderKeyRouter] = None
        self.stats: Dict[str, int] = {}
        self.last_fulltext_errors: Dict[str, str] = {}
        self._fulltext_cache: Dict[str, str] = {}  # url -> markdown

    def _build_jina_router(self) -> ProviderKeyRouter:
        return ProviderKeyRouter(
            "jina", self._jina_keys, min_interval_seconds=0.0
        )

    def _build_firecrawl_router(self) -> ProviderKeyRouter:
        return ProviderKeyRouter(
            "firecrawl", self._firecrawl_keys, min_interval_seconds=0.0
        )

    def _jina_router_instance(self) -> ProviderKeyRouter:
        if self._jina_router is None:
            self._jina_router = self._build_jina_router()
        return self._jina_router

    def _firecrawl_router_instance(self) -> ProviderKeyRouter:
        if self._firecrawl_router is None:
            self._firecrawl_router = self._build_firecrawl_router()
        return self._firecrawl_router

    def search(
        self,
        query: str,
        backends: List[str] | None = None,
        max_results: int = 5,
    ) -> List[Dict[str, Any]]:
        """Search across multiple backends. Returns unified SourceRecord list."""
        if backends is None:
            backends = ["tavily", "arxiv", "duckduckgo"]

        all_results = []
        for name in backends:
            try:
                results = self._cached_or_live_search(name, query, max_results)
                self.stats[name] = self.stats.get(name, 0) + len(results)
                all_results.extend(results)
            except Exception as e:
                self.stats[f"{name}_errors"] = self.stats.get(f"{name}_errors", 0) + 1

        # Dedup by source_id or title
        seen = set()
        deduped = []
        for s in all_results:
            key = s.get("source_id") or s.get("title", "")[:80]
            if key and key not in seen:
                seen.add(key)
                deduped.append(s)
        return deduped

    def _cached_or_live_search(self, backend: str, query: str, max_results: int) -> List[Dict]:
        """Use persistent local cache before spending external search quota."""

        cache_enabled = os.environ.get("OPTOMIND_DISABLE_SEARCH_CACHE", "").strip().lower() not in {
            "1", "true", "yes", "on",
        }
        min_similarity = float(os.environ.get("OPTOMIND_SEARCH_CACHE_SIMILARITY", "0.58"))
        if cache_enabled:
            try:
                from optomind_research.cache import get_cache

                cached, meta = get_cache().get_cached_search_results(
                    query,
                    backend,
                    max_results=max_results,
                    min_similarity=min_similarity,
                )
                if cached:
                    self.stats["search_cache_hits"] = self.stats.get("search_cache_hits", 0) + 1
                    self.stats[f"{backend}_cache_hits"] = self.stats.get(f"{backend}_cache_hits", 0) + 1
                    return cached
                self.stats["search_cache_misses"] = self.stats.get("search_cache_misses", 0) + 1
            except Exception:
                self.stats["search_cache_errors"] = self.stats.get("search_cache_errors", 0) + 1

        results = self._search_one(backend, query, max_results)
        if cache_enabled and results:
            try:
                from optomind_research.cache import get_cache

                get_cache().save_search_results(query, backend, results, max_results=max_results)
                get_cache().save_papers(results)
                get_cache().save_search(
                    query,
                    results,
                    [backend],
                    stats={"cache_saved": True, "backend": backend},
                )
                self.stats["search_cache_saved"] = self.stats.get("search_cache_saved", 0) + 1
            except Exception:
                self.stats["search_cache_save_errors"] = self.stats.get("search_cache_save_errors", 0) + 1
        return results

    def _search_one(self, name: str, query: str, n: int) -> List[Dict]:
        method = getattr(self, f"_search_{name}", None)
        if method:
            return method(query, n)
        return []

    # ═══════════ FUNCTIONAL BACKENDS (no key needed) ═══════════

    def _search_arxiv(self, q: str, n: int) -> List[Dict]:
        from tools.academic_backends.arxiv_backend import ArxivBackend, normalize_arxiv_result
        b = ArxivBackend()
        return [normalize_arxiv_result(s) for s in b.search(q, max_results=n)]

    def _search_crossref(self, q: str, n: int) -> List[Dict]:
        from tools.academic_backends.crossref_backend import CrossrefBackend, normalize_crossref_result
        b = CrossrefBackend()
        return [normalize_crossref_result(s) for s in b.search(q, max_results=n)]

    def _search_openalex(self, q: str, n: int) -> List[Dict]:
        from tools.academic_backends.openalex_backend import OpenAlexBackend, normalize_openalex_result
        b = OpenAlexBackend()
        return [normalize_openalex_result(s) for s in b.search(q, max_results=n)]

    def _search_duckduckgo(self, q: str, n: int) -> List[Dict]:
        """Free web search via DuckDuckGo — no API key needed."""
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            results = []
            with DDGS() as ddgs:
                for r in ddgs.text(q, max_results=n):
                    results.append({
                        "source_id": f"ddg:{r.get('href','')[:80]}",
                        "title": r.get("title", ""),
                        "url_or_doi": r.get("href", ""),
                        "source_url": r.get("href", ""),
                        "abstract_or_snippet": r.get("body", ""),
                        "backend": "duckduckgo",
                        "retrieval_method": "web_search",
                        "verification_status": "unverified",
                        "evidence_extraction_ready": bool(r.get("body")),
                        "relevance_score": 0.5,
                    })
            return results
        except ImportError:
            return []

    def _search_github(self, q: str, n: int) -> List[Dict]:
        from tools.academic_backends.github_backend import GitHubBackend
        b = GitHubBackend()
        return b.search_repos(q, max_results=n)

    def _search_semantic_scholar_public(self, q: str, n: int) -> List[Dict]:
        """Semantic Scholar — uses API key for higher rate limits if available."""
        try:
            url = f"https://api.semanticscholar.org/graph/v1/paper/search?query={urllib.parse.quote(q)}&limit={n}&fields=paperId,title,abstract,year,authors,url,externalIds,citationCount,publicationVenue,openAccessPdf"
            headers = {"User-Agent": "OptoMind/0.4"}
            if self.s2_key:
                headers["x-api-key"] = self.s2_key
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            results = []
            for p in data.get("data", []):
                ext = p.get("externalIds", {}) or {}
                venue = p.get("publicationVenue") or {}
                results.append({
                    "source_id": f"s2:{p.get('paperId','')}",
                    "title": p.get("title", ""),
                    "authors": [a.get("name","") for a in p.get("authors", [])],
                    "year": p.get("year"),
                    "doi": ext.get("DOI", ""),
                    "semantic_scholar_paper_id": p.get("paperId", ""),
                    "url_or_doi": p.get("url", "") or (f"https://doi.org/{ext['DOI']}" if ext.get("DOI") else ""),
                    "abstract_or_snippet": p.get("abstract", ""),
                    "backend": "semantic_scholar",
                    "venue": venue.get("name", "") if isinstance(venue, dict) else "",
                    "verification_status": "verified" if ext.get("DOI") else "unverified",
                    "evidence_extraction_ready": bool(p.get("abstract", "") and len(p.get("abstract","")) > 50),
                    "relevance_score": min(1.0, p.get("citationCount", 0) / 100.0),
                    "raw_metadata": {
                        "citation_count": p.get("citationCount", 0),
                        "open_access_pdf": p.get("openAccessPdf", {}),
                    },
                })
            return results
        except Exception:
            return []

    def _search_unpaywall(self, q: str, n: int) -> List[Dict]:
        from tools.academic_backends.unpaywall_backend import UnpaywallBackend
        b = UnpaywallBackend()
        info = b.lookup(q)
        return [{"source_id": f"oa:{q}", "title": info.get("title",""), "backend": "unpaywall", "is_oa": info.get("is_oa")}] if info else []

    # ═══════════ TAVILY (have key!) ═══════════

    def _search_tavily(self, q: str, n: int) -> List[Dict]:
        """Tavily Search API — AI-optimized web search. 1000 req/month free."""
        if not self.tavily_key:
            return []
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=self.tavily_key)
            try:
                resp = client.search(query=q, max_results=n, search_depth="basic", time_range="year")
                recency_sort = "time_range=year"
            except TypeError:
                resp = client.search(query=q, max_results=n, search_depth="basic")
                recency_sort = "query_suffix_only"
            results = []
            for r in resp.get("results", []):
                results.append({
                    "source_id": f"tavily:{r.get('url','')[:80]}",
                    "title": r.get("title", ""),
                    "url_or_doi": r.get("url", ""),
                    "source_url": r.get("url", ""),
                    "abstract_or_snippet": r.get("content", ""),
                    "backend": "tavily",
                    "retrieval_method": "web_search_api",
                    "verification_status": "unverified",
                    "evidence_extraction_ready": bool(r.get("content")),
                    "relevance_score": r.get("score", 0.5),
                    "raw_metadata": {"web_recency_sort": recency_sort},
                })
            return results
        except Exception:
            return []

    # ═══════════ BACKENDS WITH API KEYS (configured) ═══════════

    def _load_key(self, env_var: str, file_path: str) -> Optional[str]:
        """Load API key from env or file."""
        return load_secret(env_var, (Path(file_path).name,))

    def _search_brave(self, q: str, n: int) -> List[Dict]:
        """Brave Search API — free tier 2000 req/month."""
        key = self._load_key("BRAVE_API_KEY", "brave_key.txt")
        if not key: return []
        try:
            url = f"https://api.search.brave.com/res/v1/web/search?q={urllib.parse.quote(q)}&count={n}&freshness=py"
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "X-Subscription-Token": key
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            return [{"source_id": f"brave:{r.get('url','')[:80]}", "title": r.get("title",""),
                     "url_or_doi": r.get("url",""), "abstract_or_snippet": r.get("description",""),
                     "backend": "brave", "retrieval_method": "web_search_api",
                     "verification_status": "unverified", "evidence_extraction_ready": bool(r.get("description")),
                     "relevance_score": 0.6,
                     "raw_metadata": {"web_recency_sort": "freshness=py"}}
                    for r in data.get("web", {}).get("results", [])]
        except Exception:
            return []

    def _search_serper(self, q: str, n: int) -> List[Dict]:
        """Serper.dev — Google Search API. 2500 free queries."""
        key = self._load_key("SERPER_API_KEY", "serper-api-key.txt")
        if not key: return []
        try:
            body = json.dumps({"q": q, "num": n, "tbs": "qdr:y,sbd:1"}).encode()
            req = urllib.request.Request("https://google.serper.dev/search", data=body, headers={
                "X-API-KEY": key, "Content-Type": "application/json"
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            results = []
            for r in data.get("organic", []):
                results.append({
                    "source_id": f"serper:{r.get('link','')[:80]}",
                    "title": r.get("title", ""),
                    "url_or_doi": r.get("link", ""),
                    "source_url": r.get("link", ""),
                    "abstract_or_snippet": r.get("snippet", ""),
                    "backend": "serper", "retrieval_method": "google_search_api",
                    "verification_status": "unverified",
                    "evidence_extraction_ready": bool(r.get("snippet")),
                    "relevance_score": 0.7,
                    "raw_metadata": {"web_recency_sort": "tbs=qdr:y,sbd:1"},
                })
            return results
        except Exception:
            return []

    def _search_semantic_scholar(self, q: str, n: int) -> List[Dict]:
        """Full S2 with API key for higher rate limits."""
        if self.s2_key:
            return self._search_semantic_scholar_public(q, n)
        return self._search_semantic_scholar_public(q, n)

    def _search_core(self, q: str, n: int) -> List[Dict]:
        from tools.academic_backends.core_backend import CoreBackend
        return CoreBackend().search(q, max_results=n)

    # Placeholder stubs for remaining backends
    _search_pubmed = lambda self, q, n: []
    _search_bing = lambda self, q, n: []
    _search_serpapi = lambda self, q, n: []
    _search_searchapi = lambda self, q, n: []
    _search_google = lambda self, q, n: []  # Needs GOOGLE_API_KEY + CSE_ID

    # ═══════════ FIRECRAWL WEB SEARCH ═══════════

    def _search_firecrawl(self, q: str, n: int) -> List[Dict]:
        """Firecrawl web search — 500 pages/month free tier."""
        if not self._firecrawl_keys:
            return []
        router = self._firecrawl_router_instance()
        for _attempt in range(len(router.keys)):
            if not _router_has_usable_lane(router):
                break
            lane, _wait = router.acquire_lane()
            if lane is None:
                break
            try:
                body = json.dumps({"query": q, "limit": n}).encode()
                req = urllib.request.Request(
                    "https://api.firecrawl.dev/v1/search",
                    data=body,
                    headers={
                        "Authorization": f"Bearer {lane.key}",
                        "Content-Type": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = json.loads(resp.read().decode())
                router.reset_lane_penalty(lane)
                results = []
                for r in data.get("data", []):
                    results.append({
                        "source_id": f"fc:{r.get('url','')[:80]}",
                        "title": r.get("title", ""),
                        "url_or_doi": r.get("url", ""),
                        "source_url": r.get("url", ""),
                        "abstract_or_snippet": r.get("description", "") or r.get("content", "")[:300],
                        "backend": "firecrawl",
                        "retrieval_method": "web_search_api",
                        "verification_status": "unverified",
                        "evidence_extraction_ready": bool(r.get("content", "")),
                        "relevance_score": 0.55,
                    })
                return results
            except urllib.error.HTTPError as exc:
                _apply_lane_http_status(
                    router, lane, int(getattr(exc, "code", 0) or 0)
                )
                continue
            except Exception:
                router.cool_lane(lane, 5.0)
                continue
            finally:
                router.release_lane(lane)
        return []

    # ═══════════ FULL-TEXT FETCHERS (post-search enrichment) ═══════════

    def fetch_fulltext(self, url: str, method: str = "jina") -> str:
        """Fetch full page content as clean markdown. Methods: jina, firecrawl.

        Jina (default): prepend https://r.jina.ai/ to URL → clean markdown. Free, no rate limit.
        Firecrawl: use /v1/scrape endpoint → markdown. 500 pages/month free.
        Results cached in-memory and in SQLite.
        """
        if url in self._fulltext_cache:
            return self._fulltext_cache[url]

        # Check SQLite cache
        cached = _fulltext_cache_get(url)
        if cached:
            self._fulltext_cache[url] = cached
            return cached

        markdown = ""
        if method == "firecrawl":
            if self._firecrawl_keys:
                markdown = self._fetch_fulltext_firecrawl(url)
            if not markdown:
                markdown = self._fetch_fulltext_jina(url) if self._jina_keys else self._fetch_fulltext_jina_anonymous(url)
        elif method == "jina":
            if self._jina_keys:
                markdown = self._fetch_fulltext_jina(url)
            if not markdown:
                markdown = self._fetch_fulltext_jina_anonymous(url)
        else:
            markdown = self._fetch_fulltext_jina(url) if self._jina_keys else self._fetch_fulltext_jina_anonymous(url)

        if markdown:
            self._fulltext_cache[url] = markdown
            _fulltext_cache_set(url, markdown)
        return markdown

    @staticmethod
    def _jina_reader_url(url: str) -> str:
        url = str(url or "").strip()
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url.lstrip("/")
        return f"https://r.jina.ai/{url}"

    def _fetch_fulltext_jina(self, url: str) -> str:
        """Jina Reader API — clean markdown, routed per key lane."""
        if not self._jina_keys:
            return ""
        router = self._jina_router_instance()
        for _attempt in range(len(router.keys)):
            if not _router_has_usable_lane(router):
                break
            lane, _wait = router.acquire_lane()
            if lane is None:
                break
            try:
                jina_url = self._jina_reader_url(url)
                req = urllib.request.Request(jina_url, headers={
                    "Authorization": f"Bearer {lane.key}",
                    "Accept": "text/plain",
                    "X-Return-Format": "markdown",
                    "User-Agent": "OptoMindLiteratureResourceBuilder/1.0",
                })
                with urllib.request.urlopen(req, timeout=30) as resp:
                    markdown = resp.read().decode("utf-8", errors="replace")
                router.reset_lane_penalty(lane)
                return markdown
            except urllib.error.HTTPError as exc:
                _apply_lane_http_status(
                    router, lane, int(getattr(exc, "code", 0) or 0)
                )
                self.last_fulltext_errors[url] = f"jina:{type(exc).__name__}: {str(exc)[:220]}"
                continue
            except Exception as exc:
                router.cool_lane(lane, 5.0)
                self.last_fulltext_errors[url] = f"jina:{type(exc).__name__}: {str(exc)[:220]}"
                continue
            finally:
                router.release_lane(lane)
        return ""

    def _fetch_fulltext_jina_anonymous(self, url: str) -> str:
        """Jina Reader without API key — limited to short content."""
        try:
            jina_url = self._jina_reader_url(url)
            req = urllib.request.Request(jina_url, headers={
                "Accept": "text/plain",
                "X-Return-Format": "markdown",
                "User-Agent": "OptoMindLiteratureResourceBuilder/1.0",
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            self.last_fulltext_errors[url] = f"jina_anonymous:{type(exc).__name__}: {str(exc)[:220]}"
            return ""

    def _fetch_fulltext_firecrawl(self, url: str) -> str:
        """Firecrawl scrape — full page content as markdown."""
        if not self._firecrawl_keys:
            return ""
        router = self._firecrawl_router_instance()
        for _attempt in range(len(router.keys)):
            if not _router_has_usable_lane(router):
                break
            lane, _wait = router.acquire_lane()
            if lane is None:
                break
            try:
                body = json.dumps({"url": url, "formats": ["markdown"]}).encode()
                req = urllib.request.Request(
                    "https://api.firecrawl.dev/v1/scrape",
                    data=body,
                    headers={
                        "Authorization": f"Bearer {lane.key}",
                        "Content-Type": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode())
                markdown = data.get("data", {}).get("markdown", "")
                router.reset_lane_penalty(lane)
                return markdown
            except urllib.error.HTTPError as exc:
                _apply_lane_http_status(
                    router, lane, int(getattr(exc, "code", 0) or 0)
                )
                self.last_fulltext_errors[url] = f"firecrawl:{type(exc).__name__}: {str(exc)[:220]}"
                continue
            except Exception as exc:
                router.cool_lane(lane, 5.0)
                self.last_fulltext_errors[url] = f"firecrawl:{type(exc).__name__}: {str(exc)[:220]}"
                continue
            finally:
                router.release_lane(lane)
        return ""

    def enrich_sources(self, sources: List[Dict], top_k: int = 5) -> List[Dict]:
        """Fetch full text for top-k sources that have URLs but no abstract."""
        enriched = 0
        for s in sources[:top_k]:
            url = s.get("url_or_doi", "") or s.get("source_url", "")
            if url and not s.get("evidence_extraction_ready") and not s.get("_fulltext"):
                md = self.fetch_fulltext(url)
                if md and len(md) > 100:
                    s["_fulltext"] = md[:5000]  # Keep first 5000 chars
                    s["abstract_or_snippet"] = md[:500]
                    s["evidence_extraction_ready"] = True
                    enriched += 1
        return sources


# ═══════════ SQLite full-text cache (delegates to shared cache.py) ═══════════


def _fulltext_cache_get(url: str) -> str:
    try:
        from optomind_research.cache import get_cache
        return get_cache().get_fulltext(url) or ""
    except Exception:
        return ""


def _fulltext_cache_set(url: str, markdown: str):
    try:
        from optomind_research.cache import get_cache
        get_cache().save_fulltext(url, markdown)
    except Exception:
        pass
