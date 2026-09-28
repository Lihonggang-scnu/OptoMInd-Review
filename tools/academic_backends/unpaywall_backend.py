"""Unpaywall backend — resolve DOIs to legal OA PDFs.

Unpaywall API: https://unpaywall.org/products/api
No API key required for basic usage. Email-based polite access: CONTACT_EMAIL env var.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

UNPAYWALL_API_BASE = "https://api.unpaywall.org/v2"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_UNPAYWALL_EMAIL_FILE = PROJECT_ROOT / "api_keys" / "Unpaywall.txt"
LEGACY_UNPAYWALL_EMAIL_FILE = Path.home() / "Desktop" / "Unpaywall.txt"


def _contact_email() -> Optional[str]:
    email = os.environ.get("UNPAYWALL_EMAIL") or os.environ.get("CONTACT_EMAIL")
    if email:
        return email.strip()
    for path in (DEFAULT_UNPAYWALL_EMAIL_FILE, LEGACY_UNPAYWALL_EMAIL_FILE):
        if path.exists():
            try:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if line and "@" in line:
                        return line
            except Exception:
                pass
    return None


class UnpaywallBackend:
    """Resolve a DOI to open-access PDF locations via Unpaywall."""

    def __init__(self) -> None:
        self._last_request = 0.0
        self.contact_email = _contact_email()
        self.enabled = bool(self.contact_email)
        self.stats: Dict[str, int] = {"requests": 0, "errors": 0}
        self.last_error = ""
        self.last_status = "ready" if self.enabled else "provider_error"

    def lookup(self, doi: str) -> Optional[Dict[str, Any]]:
        if not self.contact_email:
            self.last_error = "contact_email_missing"
            self.last_status = "provider_error"
            return None
        clean_doi = doi.strip()
        url = f"{UNPAYWALL_API_BASE}/{urllib.parse.quote(clean_doi, safe='')}?email={urllib.parse.quote(self.contact_email, safe='')}"
        elapsed = time.monotonic() - self._last_request
        if elapsed < 0.2:
            time.sleep(0.2 - elapsed)
        self._last_request = time.monotonic()
        self.stats["requests"] += 1
        req = urllib.request.Request(url, headers={"User-Agent": "OptoMind/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            self.last_error = ""
            self.last_status = "ok"
        except urllib.error.HTTPError as exc:
            self.stats["errors"] += 1
            self.last_error = f"HTTP {int(getattr(exc, 'code', 0) or 0)}"
            self.last_status = "provider_error"
            return None
        except Exception as exc:
            self.stats["errors"] += 1
            self.last_error = type(exc).__name__
            self.last_status = "provider_error"
            return None

        best_oa = data.get("best_oa_location") or {}
        oa_locations = data.get("oa_locations") or []

        return {
            "doi": clean_doi,
            "title": data.get("title", ""),
            "is_oa": data.get("is_oa", False),
            "oa_status": data.get("oa_status", "closed"),
            "best_oa_url": best_oa.get("url_for_pdf") or best_oa.get("url", ""),
            "best_oa_landing_page_url": best_oa.get("url", ""),
            "best_oa_license": best_oa.get("license", ""),
            "best_oa_version": best_oa.get("version", ""),
            "oa_locations": [
                {
                    "url": loc.get("url", ""),
                    "url_for_pdf": loc.get("url_for_pdf", ""),
                    "landing_page_url": loc.get("url", ""),
                    "host_type": loc.get("host_type", ""),
                    "license": loc.get("license", ""),
                    "version": loc.get("version", ""),
                    "is_oa": loc.get("is_oa", data.get("is_oa", False)),
                }
                for loc in oa_locations[:5]
            ],
        }
