"""Institutional access through a reusable Playwright browser session.

This backend is deliberately conservative:

- disabled unless the caller opts in;
- does not read or log passwords;
- opens a real browser for manual university / publisher login;
- stores only the local browser profile and Playwright storage state;
- reuses that session to fetch pages or PDFs the user is authorized to access.

It is not a paywall bypasser, crawler, Sci-Hub client, or credential bot.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from urllib.parse import urlparse
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE_DIR = PROJECT_ROOT / "literature_workspace" / "browser_profiles" / "scnu"
DEFAULT_EDGE_CDP_PROFILE_DIR = PROJECT_ROOT / "literature_workspace" / "browser_profiles" / "edge_cdp_scnu"
DEFAULT_DOWNLOAD_DIR = PROJECT_ROOT / "literature_workspace" / "institution_downloads"
DEFAULT_SCNU_LIBVPN_LOGIN_URL = "https://libvpn.scnu.edu.cn/portal/?redirect_uri=https%3A%2F%2Flib-scnu-edu-cn-s.libvpn.scnu.edu.cn%3A20080%2F#!/login"


@dataclass
class BrowserFetchResult:
    ok: bool
    url: str
    final_url: str = ""
    status: int = 0
    content_type: str = ""
    local_file_path: str = ""
    text: str = ""
    bytes_written: int = 0
    access_method: str = "institution_playwright"
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class InstitutionalAccessBackend:
    """Institution-authenticated page/PDF fetcher based on a saved browser profile."""

    def __init__(
        self,
        profile_dir: str | Path | None = None,
        *,
        enabled: bool = False,
        headless: bool = True,
        browser_channel: str = "msedge",
        cdp_endpoint: str = "http://127.0.0.1:9222",
        downloads_dir: str | Path | None = None,
        storage_state_path: str | Path | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.profile_dir = Path(profile_dir) if profile_dir else DEFAULT_PROFILE_DIR
        self.downloads_dir = Path(downloads_dir) if downloads_dir else DEFAULT_DOWNLOAD_DIR
        self.storage_state_path = Path(storage_state_path) if storage_state_path else self.profile_dir / "storage_state.json"
        self.headless = bool(headless)
        self.browser_channel = str(browser_channel or "msedge").strip()
        self.cdp_endpoint = str(cdp_endpoint or "http://127.0.0.1:9222").strip().rstrip("/")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self._playwright_available = self._check_playwright_import()
        self._browser_executable_present = self._check_browser_executable()
        self.stats: Dict[str, int] = {"pdfs_downloaded": 0, "html_pages_fetched": 0, "errors": 0}

    @staticmethod
    def _check_playwright_import() -> bool:
        try:
            import importlib

            importlib.import_module("playwright.sync_api")
            return True
        except Exception:
            return False

    def _check_browser_executable(self) -> bool:
        if not self._playwright_available:
            return False
        if self.browser_channel in {"edge-cdp", "cdp"}:
            return self._check_cdp_available()
        if self.browser_channel in {"msedge", "msedge-beta", "msedge-dev", "msedge-canary"}:
            edge_paths = [
                Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
                Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
            ]
            return any(path.exists() for path in edge_paths)
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                return Path(pw.chromium.executable_path).exists()
        except Exception:
            return False

    def check_status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "playwright_available": self._playwright_available,
            "browser_executable_present": self._browser_executable_present,
            "browser_channel": self.browser_channel,
            "cdp_endpoint": self.cdp_endpoint if self.browser_channel in {"edge-cdp", "cdp"} else "",
            "cdp_available": self._check_cdp_available() if self.browser_channel in {"edge-cdp", "cdp"} else False,
            "profile_dir": str(self.profile_dir),
            "edge_cdp_profile_dir": str(DEFAULT_EDGE_CDP_PROFILE_DIR),
            "default_scnu_libvpn_login_url": DEFAULT_SCNU_LIBVPN_LOGIN_URL,
            "downloads_dir": str(self.downloads_dir),
            "storage_state_path": str(self.storage_state_path),
            "storage_state_exists": self.storage_state_path.exists(),
            "never_log_credentials": True,
            "mode": "playwright_manual_login_then_session_reuse",
            "install_browser_command": "py -3.11 -m playwright install chromium",
        }

    def _check_cdp_available(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.cdp_endpoint}/json/version", timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _launch_persistent_context(self, pw: Any, *, headless: bool, downloads_path: str | Path | None = None) -> Any:
        launch_kwargs = {
            "user_data_dir": str(self.profile_dir),
            "headless": headless,
            "accept_downloads": True,
            "downloads_path": str(downloads_path or self.downloads_dir),
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }
        if self.browser_channel and self.browser_channel != "chromium":
            launch_kwargs["channel"] = self.browser_channel
        return pw.chromium.launch_persistent_context(**launch_kwargs)

    def manual_login_session(
        self,
        *,
        start_url: str = "https://www.google.com",
        timeout_seconds: int = 900,
        wait_for_enter: bool = True,
    ) -> Dict[str, Any]:
        """Open a headed browser and let the user log in manually.

        The user may navigate to the university library portal, CARSI,
        Shibboleth, VPN page, or publisher page. When the user presses Enter
        in the terminal, or the timeout expires, cookies/storage are saved.
        """
        if not self.enabled:
            return {"ok": False, "error": "institutional access is disabled"}
        if not self._playwright_available:
            return {"ok": False, "error": "playwright is not installed"}
        if self.browser_channel in {"edge-cdp", "cdp"}:
            return self.open_edge_cdp_session(start_url=start_url or DEFAULT_SCNU_LIBVPN_LOGIN_URL)
        if not self._browser_executable_present:
            return {"ok": False, "error": "chromium browser is not installed", "install_browser_command": "py -3.11 -m playwright install chromium"}

        from playwright.sync_api import sync_playwright

        started = time.time()
        with sync_playwright() as pw:
            context = self._launch_persistent_context(pw, headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(start_url or "https://www.google.com", wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                pass
            if wait_for_enter:
                print()
                print("[OptoMind] Browser opened for manual institution login.")
                print("[OptoMind] Complete login in the browser, then press Enter here to save the session.")
                print("[OptoMind] Credentials are not read, printed, or stored by OptoMind.")
                try:
                    input()
                except EOFError:
                    self._wait_until_closed_or_timeout(context, timeout_seconds)
            else:
                self._wait_until_closed_or_timeout(context, timeout_seconds)
            state = context.storage_state()
            self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
            self.storage_state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            pages = [p.url for p in context.pages]
            context.close()
        return {
            "ok": True,
            "profile_dir": str(self.profile_dir),
            "storage_state_path": str(self.storage_state_path),
            "storage_state_exists": self.storage_state_path.exists(),
            "elapsed_seconds": round(time.time() - started, 2),
            "last_pages": pages[-5:],
        }

    @staticmethod
    def _edge_executable_path() -> Path | None:
        edge_paths = [
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        ]
        return next((path for path in edge_paths if path.exists()), None)

    def _cdp_port(self) -> str:
        match = re.search(r":(\d+)(?:/|$)", self.cdp_endpoint)
        return match.group(1) if match else "9222"

    def open_edge_cdp_session(self, *, start_url: str = DEFAULT_SCNU_LIBVPN_LOGIN_URL) -> Dict[str, Any]:
        """Open or reuse the real Edge-CDP browser for SCNU library login.

        This does not read, print, or store credentials. It only opens the
        library VPN login page in the dedicated Edge user-data directory so the
        browser can remember the user's account/password according to Edge's own
        password manager and cookie policy.
        """
        if not self.enabled:
            return {"ok": False, "error": "institutional access is disabled"}
        if not self._playwright_available:
            return {"ok": False, "error": "playwright is not installed"}

        started_process = False
        start_url = start_url or DEFAULT_SCNU_LIBVPN_LOGIN_URL
        if not self._check_cdp_available():
            edge = self._edge_executable_path()
            if not edge:
                return {"ok": False, "error": "msedge.exe not found"}
            DEFAULT_EDGE_CDP_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(
                [
                    str(edge),
                    f"--remote-debugging-port={self._cdp_port()}",
                    f"--user-data-dir={DEFAULT_EDGE_CDP_PROFILE_DIR}",
                    "--new-window",
                    start_url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            started_process = True
            for _ in range(20):
                if self._check_cdp_available():
                    break
                time.sleep(0.5)
        if not self._check_cdp_available():
            return {"ok": False, "error": f"Edge CDP is not available at {self.cdp_endpoint}"}

        pages: list[str] = []
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(self.cdp_endpoint)
                context = browser.contexts[0] if browser.contexts else browser.new_context(accept_downloads=True)
                existing_pages = [page for page in context.pages if not page.is_closed()]
                if not any("libvpn.scnu.edu.cn" in (page.url or "") for page in existing_pages):
                    page = context.new_page()
                    try:
                        page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
                    except Exception:
                        pass
                    existing_pages = [*existing_pages, page]
                pages = [page.url for page in existing_pages if not page.is_closed()]
        except Exception as exc:
            return {
                "ok": True,
                "started_process": started_process,
                "warning": f"Edge launched but page inspection failed: {type(exc).__name__}",
                "cdp_endpoint": self.cdp_endpoint,
                "edge_cdp_profile_dir": str(DEFAULT_EDGE_CDP_PROFILE_DIR),
                "start_url": start_url,
            }

        return {
            "ok": True,
            "started_process": started_process,
            "cdp_endpoint": self.cdp_endpoint,
            "cdp_available": self._check_cdp_available(),
            "edge_cdp_profile_dir": str(DEFAULT_EDGE_CDP_PROFILE_DIR),
            "start_url": start_url,
            "last_pages": pages[-8:],
            "never_log_credentials": True,
            "message": "Complete or refresh SCNU library login in the opened Edge window; keep it open for publisher HTML fetching.",
        }

    @staticmethod
    def _wait_until_closed_or_timeout(context: Any, timeout_seconds: int) -> None:
        deadline = time.time() + min(max(timeout_seconds, 5), 900)
        while time.time() < deadline:
            try:
                pages = context.pages
                if not pages or all(page.is_closed() for page in pages):
                    break
                pages[0].wait_for_timeout(1000)
            except Exception:
                break

    def fetch_url(
        self,
        url: str,
        *,
        output_dir: str | Path | None = None,
        filename_stem: str = "institution_fetch",
        expect: str = "auto",
        timeout_ms: int = 60_000,
    ) -> BrowserFetchResult:
        """Fetch a URL through the saved browser session.

        expect: auto | pdf | html
        """
        if not self.enabled:
            return BrowserFetchResult(ok=False, url=url, error="institutional access is disabled")
        if not self._playwright_available:
            return BrowserFetchResult(ok=False, url=url, error="playwright is not installed")
        if not self._browser_executable_present:
            if self.browser_channel in {"edge-cdp", "cdp"}:
                return BrowserFetchResult(ok=False, url=url, error=f"Edge CDP is not available at {self.cdp_endpoint}; start Edge with --remote-debugging-port=9222")
            return BrowserFetchResult(ok=False, url=url, error="browser executable is not available")
        if not url or not re.match(r"^https?://", str(url), re.I):
            return BrowserFetchResult(ok=False, url=url, error="invalid URL")

        output_path = Path(output_dir) if output_dir else self.downloads_dir
        output_path.mkdir(parents=True, exist_ok=True)
        safe_stem = re.sub(r"[^0-9A-Za-z_.-]+", "-", filename_stem or "institution_fetch").strip("-")[:120] or "institution_fetch"

        from playwright.sync_api import sync_playwright

        try:
            if self.browser_channel in {"edge-cdp", "cdp"}:
                return self._fetch_url_via_cdp(url, output_path=output_path, safe_stem=safe_stem, expect=expect, timeout_ms=timeout_ms)
            with sync_playwright() as pw:
                context = self._launch_persistent_context(pw, headless=self.headless, downloads_path=output_path)
                try:
                    result = self._fetch_with_context_request(context, url, output_path, safe_stem, expect, timeout_ms)
                    if result.ok and not self._needs_page_navigation_fallback(result):
                        return result
                    # Some publishers require a page navigation to refresh cookies.
                    page_result = self._fetch_with_page(context, url, output_path, safe_stem, expect, timeout_ms)
                    return page_result if page_result.ok else result
                finally:
                    try:
                        state = context.storage_state()
                        self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                        self.storage_state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                    except Exception:
                        pass
                    context.close()
        except Exception as exc:
            self.stats["errors"] += 1
            return BrowserFetchResult(ok=False, url=url, error=f"institution fetch failed: {type(exc).__name__}")

    def _fetch_url_via_cdp(self, url: str, *, output_path: Path, safe_stem: str, expect: str, timeout_ms: int) -> BrowserFetchResult:
        from playwright.sync_api import sync_playwright

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(self.cdp_endpoint)
                context = browser.contexts[0] if browser.contexts else browser.new_context(accept_downloads=True)
                active_page_result = self._fetch_from_matching_existing_page(context, url, output_path, safe_stem, expect, timeout_ms)
                if active_page_result.ok:
                    active_page_result.access_method = f"{active_page_result.access_method}+edge_cdp_active_page"
                    return active_page_result
                # In CDP mode, publisher HTML is the primary route. Avoid using
                # APIRequestContext for auto/html fetches because libvpn publisher
                # domains may fail certificate validation there while the real
                # browser page can still render the authorized HTML correctly.
                if expect == "pdf":
                    try:
                        req_result = self._fetch_with_context_request(context, url, output_path, safe_stem, expect, timeout_ms)
                    except Exception as exc:
                        req_result = BrowserFetchResult(ok=False, url=url, error=f"context request failed before page fallback: {type(exc).__name__}")
                    if req_result.ok:
                        req_result.access_method = f"{req_result.access_method}+edge_cdp_request"
                        return req_result
                page_result = self._fetch_with_page(context, url, output_path, safe_stem, expect, timeout_ms)
                if page_result.ok:
                    page_result.access_method = f"{page_result.access_method}+edge_cdp"
                    return page_result
                # If page navigation failed, try an authenticated request in the same context.
                try:
                    req_result = self._fetch_with_context_request(context, url, output_path, safe_stem, expect, timeout_ms)
                except Exception as exc:
                    req_result = BrowserFetchResult(ok=False, url=url, error=f"context request failed: {type(exc).__name__}")
                if req_result.ok:
                    req_result.access_method = f"{req_result.access_method}+edge_cdp"
                    return req_result
                return page_result if page_result.error else req_result
        except Exception as exc:
            self.stats["errors"] += 1
            return BrowserFetchResult(ok=False, url=url, error=f"edge CDP fetch failed: {type(exc).__name__}")

    @staticmethod
    def _url_match_key(url: str) -> tuple[str, str]:
        parsed = urlparse(url or "")
        return (parsed.netloc.lower(), parsed.path.rstrip("/").lower())

    @classmethod
    def _urls_refer_to_same_page(cls, left: str, right: str) -> bool:
        left_host, left_path = cls._url_match_key(left)
        right_host, right_path = cls._url_match_key(right)
        if not left_host or not right_host:
            return False
        if left_host != right_host:
            return False
        if left_path == right_path:
            return True
        # Publisher pages sometimes add a trailing route or remove a suffix.
        # Only allow this loose match for DOI article paths, not for generic
        # journal/search pages.
        if "/doi/" in left_path and "/doi/" in right_path:
            return left_path.startswith(right_path) or right_path.startswith(left_path)
        return False

    def _fetch_from_matching_existing_page(
        self,
        context: Any,
        url: str,
        output_dir: Path,
        safe_stem: str,
        expect: str,
        timeout_ms: int,
    ) -> BrowserFetchResult:
        if expect == "pdf":
            return BrowserFetchResult(ok=False, url=url, error="active page reuse skipped for pdf")
        for page in list(getattr(context, "pages", []) or []):
            try:
                if page.is_closed() or not self._urls_refer_to_same_page(page.url, url):
                    continue
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 5_000))
                except Exception:
                    pass
                html = page.content()
                return self._materialize_response(
                    url,
                    page.url,
                    200,
                    "text/html",
                    html.encode("utf-8", errors="replace"),
                    output_dir,
                    safe_stem,
                    expect,
                )
            except Exception:
                continue
        return BrowserFetchResult(ok=False, url=url, error="no matching active publisher page")

    @staticmethod
    def _looks_like_institution_login(final_url: str, text: str = "") -> bool:
        lowered_url = (final_url or "").lower()
        sample = (text or "")[:80_000].lower()
        if "libvpn.scnu.edu.cn/portal" in lowered_url:
            return True
        if "华南师范大学图书馆vpn登录界面" in sample:
            return True
        portal_shell = "portal-section" in sample or "sangfor" in sample or "libvpn.scnu.edu.cn/portal" in sample
        login_hint = any(marker in sample for marker in ["vpn登录", "账号登录", "统一身份认证", "用户名", "忘记密码"])
        return bool(portal_shell and login_hint)

    @staticmethod
    def _needs_page_navigation_fallback(result: BrowserFetchResult) -> bool:
        if not result.ok:
            return True
        if InstitutionalAccessBackend._looks_like_institution_login(result.final_url or result.url, result.text):
            return True
        if result.local_file_path.lower().endswith(".pdf"):
            return False
        text = (result.text or "").strip()
        lowered = text.lower()
        if len(text) < 5000 and any(marker in lowered for marker in ["<title>redirecting", "redirecting", "document.location", "window.location", "http-equiv=\"refresh", "please wait"]):
            return True
        if result.url.lower().startswith("https://doi.org/") and len(text) < 8000:
            return True
        return False

    def _fetch_with_context_request(self, context: Any, url: str, output_dir: Path, safe_stem: str, expect: str, timeout_ms: int) -> BrowserFetchResult:
        response = context.request.get(
            url,
            timeout=timeout_ms,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; OptoMindInstitutionSession/1.0)",
                "Accept": "application/pdf,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            max_redirects=8,
        )
        status = int(response.status)
        content_type = str(response.headers.get("content-type") or "")
        final_url = str(response.url or url)
        if not response.ok:
            return BrowserFetchResult(ok=False, url=url, final_url=final_url, status=status, content_type=content_type, error=f"HTTP {status}")
        body = response.body()
        return self._materialize_response(url, final_url, status, content_type, body, output_dir, safe_stem, expect)

    def _fetch_with_page(self, context: Any, url: str, output_dir: Path, safe_stem: str, expect: str, timeout_ms: int) -> BrowserFetchResult:
        page = context.new_page()
        download_obj = None
        if expect == "pdf":
            try:
                with page.expect_download(timeout=timeout_ms) as download_info:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                download_obj = download_info.value
            except Exception:
                download_obj = None
            if download_obj is not None:
                suggested = download_obj.suggested_filename or f"{safe_stem}.pdf"
                suffix = Path(suggested).suffix or ".pdf"
                target = output_dir / f"{safe_stem}{suffix}"
                download_obj.save_as(str(target))
                self.stats["pdfs_downloaded"] += 1
                return BrowserFetchResult(ok=True, url=url, final_url=page.url, status=200, content_type="application/pdf", local_file_path=str(target), bytes_written=target.stat().st_size, access_method="institution_playwright_download")
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15_000))
            except Exception:
                pass
            html = page.content()
            final_url = page.url
            status = int(response.status) if response else 200
            content_type = str(response.headers.get("content-type") or "text/html") if response else "text/html"
            return self._materialize_response(url, final_url, status, content_type, html.encode("utf-8", errors="replace"), output_dir, safe_stem, expect)
        except Exception as exc:
            return BrowserFetchResult(ok=False, url=url, final_url=page.url if page else "", error=f"page fetch failed: {type(exc).__name__}")
        finally:
            try:
                page.close()
            except Exception:
                pass

    def _materialize_response(
        self,
        url: str,
        final_url: str,
        status: int,
        content_type: str,
        body: bytes,
        output_dir: Path,
        safe_stem: str,
        expect: str,
    ) -> BrowserFetchResult:
        is_pdf = body[:4] == b"%PDF" or "pdf" in content_type.lower()
        if expect == "pdf" and not is_pdf:
            text_preview = body[:1200].decode("utf-8", errors="replace")
            if self._looks_like_institution_login(final_url, text_preview):
                self.stats["errors"] += 1
                return BrowserFetchResult(ok=False, url=url, final_url=final_url, status=status, content_type=content_type, text=text_preview, error="institution_session_login_required")
            return BrowserFetchResult(ok=False, url=url, final_url=final_url, status=status, content_type=content_type, text=text_preview, error="session response is not a PDF")
        if is_pdf:
            path = output_dir / f"{safe_stem}.pdf"
            path.write_bytes(body)
            self.stats["pdfs_downloaded"] += 1
            return BrowserFetchResult(ok=True, url=url, final_url=final_url, status=status, content_type=content_type or "application/pdf", local_file_path=str(path), bytes_written=len(body), access_method="institution_playwright_pdf")
        text = body.decode("utf-8", errors="replace")
        if self._looks_like_institution_login(final_url, text):
            self.stats["errors"] += 1
            return BrowserFetchResult(ok=False, url=url, final_url=final_url, status=status, content_type=content_type, text=text[:1200], error="institution_session_login_required")
        if expect == "html" or expect == "auto":
            path = output_dir / f"{safe_stem}.institution.html"
            path.write_text(text, encoding="utf-8")
            self.stats["html_pages_fetched"] += 1
            return BrowserFetchResult(ok=True, url=url, final_url=final_url, status=status, content_type=content_type or "text/html", local_file_path=str(path), text=text, bytes_written=len(body), access_method="institution_playwright_html")
        return BrowserFetchResult(ok=False, url=url, final_url=final_url, status=status, content_type=content_type, text=text[:1200], error="unexpected response type")

    def download_pdf_via_session(self, pdf_url: str, output_dir: str | Path, *, filename_stem: str = "institution_pdf") -> Optional[str]:
        result = self.fetch_url(pdf_url, output_dir=output_dir, filename_stem=filename_stem, expect="pdf")
        return result.local_file_path if result.ok and result.local_file_path else None
