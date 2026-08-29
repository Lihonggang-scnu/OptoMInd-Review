"""GitHub repository search backend.

Searches for high-star open-source projects relevant to:
- TMM (transfer matrix method)
- thin-film optimization
- photonic inverse design
- AI for science / materials

No API key required for basic search (60 req/hour without token).
GITHUB_TOKEN env var increases rate limit to 5000 req/hour.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

GITHUB_API_BASE = "https://api.github.com"


def _github_token() -> Optional[str]:
    return os.environ.get("GITHUB_TOKEN") or None


class GitHubBackend:
    """Search GitHub repositories for relevant open-source projects."""

    def __init__(self) -> None:
        self._last_request = 0.0
        self._has_token = bool(_github_token())
        self.stats: Dict[str, int] = {"requests": 0, "errors": 0}

    def search_repos(self, query: str, max_results: int = 10, sort: str = "stars") -> List[Dict[str, Any]]:
        """Search GitHub repositories."""
        params = {"q": query, "sort": sort, "order": "desc", "per_page": str(min(max_results, 30))}
        url = f"{GITHUB_API_BASE}/search/repositories?{urllib.parse.urlencode(params)}"
        data = self._fetch_json(url)
        if data is None:
            return []

        results = []
        for item in data.get("items", []):
            results.append({
                "source_id": f"github:{item.get('full_name', '')}",
                "title": item.get("full_name", ""),
                "description": item.get("description", ""),
                "stars": item.get("stargazers_count", 0),
                "forks": item.get("forks_count", 0),
                "language": item.get("language", ""),
                "url_or_doi": item.get("html_url", ""),
                "source_url": item.get("html_url", ""),
                "topics": item.get("topics", []),
                "license": (item.get("license") or {}).get("spdx_id", ""),
                "updated_at": item.get("updated_at", ""),
                "backend": "github",
                "retrieval_method": "github_api",
                "verification_status": "verified_url",
                "evidence_extraction_ready": bool(item.get("description")),
                "relevance_score": min(1.0, item.get("stargazers_count", 0) / 500.0),
                "raw_metadata": {
                    "open_issues": item.get("open_issues_count", 0),
                    "created_at": item.get("created_at", ""),
                    "default_branch": item.get("default_branch", ""),
                },
            })
        return results

    def _fetch_json(self, url: str) -> Optional[Dict[str, Any]]:
        elapsed = time.monotonic() - self._last_request
        if elapsed < 1.0 and not self._has_token:
            time.sleep(1.0 - elapsed)
        self._last_request = time.monotonic()
        self.stats["requests"] += 1

        headers = {"User-Agent": "OptoMind/0.1", "Accept": "application/vnd.github.v3+json"}
        token = _github_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            self.stats["errors"] += 1
            return None
