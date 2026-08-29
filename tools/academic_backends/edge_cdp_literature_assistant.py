"""Legal literature fulltext helpers for an already logged-in Edge-CDP session.

The goal is to behave like a cautious browser assistant:
- inspect real browser pages before deciding a route;
- classify page type instead of assuming every URL is a PDF;
- save already-open publisher HTML/text when it is a usable article page;
- open human-in-loop pages with clear download guidance when automation fails.

No grey sources are used here.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup


DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9222"


@dataclass
class BrowserPageSnapshot:
    index: int
    title: str
    url: str
    text_chars: int
    page_type: str
    publisher: str
    doi_candidates: list[str]
    reason: str


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def safe_doi_stem(doi: str, fallback: str = "paper") -> str:
    if doi:
        return "DOI_" + re.sub(r"[^A-Za-z0-9]+", "_", doi.lower()).strip("_")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", fallback or "paper").strip("_")[:120] or "paper"


def extract_text_from_html(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for bad in soup(["script", "style", "noscript", "svg"]):
        bad.decompose()
    return normalize_space(soup.get_text(" ", strip=True))


def detect_publisher(url: str) -> str:
    u = str(url or "").lower()
    if "wiley.com" in u or "onlinelibrary" in u:
        return "wiley"
    if "nature.com" in u or "springer.com" in u:
        return "springer_nature"
    if "cell.com" in u:
        return "cell"
    if "sciencedirect.com" in u or "elsevier.com" in u:
        return "elsevier"
    if "pubs.aip.org" in u:
        return "aip"
    if "pubs.acs.org" in u:
        return "acs"
    if "science.org" in u:
        return "science"
    if "ssrn.com" in u:
        return "ssrn"
    if "osti.gov" in u:
        return "repository_osti"
    if "libvpn.scnu.edu.cn" in u or "scnu.edu.cn" in u:
        return "scnu_library_or_vpn"
    return "unknown"


def doi_candidates(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in re.finditer(r"10\.\d{4,9}/[^\s\"'<>]+", text or "", flags=re.I):
        doi = match.group(0).strip().rstrip(").,;]")
        key = doi.lower()
        if key not in seen:
            seen.add(key)
            out.append(doi)
        if len(out) >= 5:
            break
    return out


def classify_page(url: str, title: str, text: str) -> tuple[str, str]:
    low = f"{title} {url} {text[:4000]}".lower()
    chars = len(text or "")
    if any(x in low for x in ["请稍候", "just a moment", "cloudflare", "__cf_chl", "captcha"]):
        return "cloudflare_or_captcha", "Cloudflare/CAPTCHA signal"
    if any(x in low for x in ["vpn登录界面", "login", "sign in", "institutional login", "access through your institution"]):
        if chars < 12000:
            return "login_or_institution_gate", "login/institution page signal"
    if any(x in low for x in ["your browser does not support javascript"]):
        return "blocked_or_js_required", "JavaScript-required blocking page"
    if re.search(r"/article-abstract/", url or "", flags=re.I) or "buy this article" in low:
        return "abstract_or_metadata_page", "abstract/buy page signal"
    if "abstract" in low and count_article_section_hits(text) <= 1:
        return "abstract_or_metadata_page", "abstract/metadata signal"
    if chars >= 18000 and count_article_section_hits(text) >= 2:
        return "fulltext_or_substantial_article_html", "long article-like page with section signals"
    if chars >= 8000 and count_article_section_hits(text) >= 1:
        return "partial_fulltext_or_article_page", "article-like page but may be partial"
    if chars >= 3000:
        return "metadata_or_background_page", "not fulltext, but has useful metadata/background"
    return "too_short_or_unknown", "short page or unknown type"


def count_article_section_hits(text: str) -> int:
    low = (text or "").lower()
    return sum(1 for marker in ["abstract", "introduction", "results", "discussion", "methods", "conclusion", "references"] if marker in low)


def publisher_route_candidates(doi: str, *, title: str = "", pii: str = "") -> list[dict[str, str]]:
    doi = str(doi or "").strip().lower()
    pii = str(pii or "").strip()
    routes: list[dict[str, str]] = []
    if not doi:
        return routes

    def add(url: str, route_type: str, note: str) -> None:
        routes.append({"url": url, "route_type": route_type, "note": note})

    add(f"https://doi.org/{doi}", "doi_resolver", "generic DOI landing page")
    if doi.startswith("10.1002/"):
        add(f"https://onlinelibrary.wiley.com/doi/{doi}", "publisher_html", "Wiley article HTML")
        add(f"https://onlinelibrary.wiley.com/doi/epdf/{doi}", "publisher_epdf", "Wiley ePDF viewer; may need human click")
    elif doi.startswith("10.1038/"):
        add(f"https://www.nature.com/articles/{doi.split('/')[-1]}", "publisher_html", "Nature/Springer article HTML")
    elif doi.startswith("10.1016/"):
        if pii:
            add(f"https://www.sciencedirect.com/science/article/pii/{pii}", "publisher_html", "ScienceDirect article page")
            add(f"https://www.sciencedirect.com/science/article/pii/{pii}/pdf", "publisher_pdf", "ScienceDirect PDF viewer; often needs browser session")
        add(f"https://doi.org/{doi}", "doi_resolver", "Elsevier/Cell DOI fallback")
    elif doi.startswith("10.1063/"):
        add(f"https://pubs.aip.org/aip/article-lookup/doi/{doi}", "publisher_html", "AIP DOI lookup")
        add(f"https://pubs.aip.org/aip/article-pdf/doi/{doi}", "publisher_pdf", "AIP PDF route; often Cloudflare protected")
    elif doi.startswith("10.1021/"):
        add(f"https://pubs.acs.org/doi/{doi}", "publisher_html", "ACS article page")
        add(f"https://pubs.acs.org/doi/pdf/{doi}", "publisher_pdf", "ACS PDF route; may require institution access")
    elif doi.startswith("10.1007/"):
        add(f"https://link.springer.com/article/{doi}", "publisher_html", "Springer article HTML")
    return routes


def inspect_edge_pages(cdp_endpoint: str = DEFAULT_CDP_ENDPOINT) -> list[BrowserPageSnapshot]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return [
            BrowserPageSnapshot(
                index=-1,
                title="",
                url="",
                text_chars=0,
                page_type="cdp_unavailable",
                publisher="unknown",
                doi_candidates=[],
                reason=(
                    f"Cannot inspect Edge CDP at {cdp_endpoint}: "
                    f"{type(exc).__name__}. Install Playwright and its "
                    "Chromium support before using the browser-assisted route. "
                    "Start the dedicated Edge login session, complete "
                    "institution login if needed, and keep the window open."
                ),
            )
        ]

    snapshots: list[BrowserPageSnapshot] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_endpoint)
            context = browser.contexts[0] if browser.contexts else None
            if not context:
                return []
            for index, page in enumerate(context.pages):
                try:
                    title = page.title()
                    url = page.url
                    text = extract_text_from_html(page.content())
                    page_type, reason = classify_page(url, title, text)
                    snapshots.append(
                        BrowserPageSnapshot(
                            index=index,
                            title=title,
                            url=url,
                            text_chars=len(text),
                            page_type=page_type,
                            publisher=detect_publisher(url),
                            doi_candidates=doi_candidates(f"{url} {title} {text[:5000]}"),
                            reason=reason,
                        )
                    )
                except Exception as exc:
                    snapshots.append(
                        BrowserPageSnapshot(
                            index=index,
                            title="",
                            url="",
                            text_chars=0,
                            page_type="inspect_failed",
                            publisher="unknown",
                            doi_candidates=[],
                            reason=f"{type(exc).__name__}: {exc}",
                        )
                    )
    except Exception as exc:
        return [
            BrowserPageSnapshot(
                index=-1,
                title="",
                url="",
                text_chars=0,
                page_type="cdp_unavailable",
                publisher="unknown",
                doi_candidates=[],
                reason=(
                    f"Cannot connect to Edge CDP at {cdp_endpoint}: {type(exc).__name__}. "
                    "Start the dedicated Edge login session, complete institution login if needed, and keep the window open."
                ),
            )
        ]
    return snapshots


def save_open_article_pages(
    *,
    output_dir: str | Path,
    cdp_endpoint: str = DEFAULT_CDP_ENDPOINT,
    min_chars: int = 8000,
    accepted_types: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Save already-open article-like pages as HTML and normalized text."""

    from playwright.sync_api import sync_playwright

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    accepted_types = accepted_types or {"fulltext_or_substantial_article_html", "partial_fulltext_or_article_page"}
    results: list[dict[str, Any]] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_endpoint)
            context = browser.contexts[0] if browser.contexts else None
            if not context:
                return []
            for index, page in enumerate(context.pages):
                try:
                    title = page.title()
                    url = page.url
                    html = page.content()
                    text = extract_text_from_html(html)
                    page_type, reason = classify_page(url, title, text)
                    dois = doi_candidates(f"{url} {title} {text[:5000]}")
                    if len(text) < int(min_chars) or page_type not in accepted_types:
                        results.append({"index": index, "saved": False, "page_type": page_type, "reason": reason, "title": title, "url": url})
                        continue
                    stem = safe_doi_stem(dois[0] if dois else "", f"page_{index}_{title[:40]}")
                    html_path = output_dir / f"{stem}.publisher.html"
                    txt_path = output_dir / f"{stem}.publisher.txt"
                    meta_path = output_dir / f"{stem}.source.json"
                    html_path.write_text(html, encoding="utf-8", errors="replace")
                    txt_path.write_text(text, encoding="utf-8", errors="replace")
                    meta = {
                        "doi": dois[0] if dois else "",
                        "title": title,
                        "source_url": url,
                        "format": "publisher_html",
                        "text_chars": len(text),
                        "page_type": page_type,
                        "publisher": detect_publisher(url),
                        "saved_by": "edge_cdp_live_page_saver",
                    }
                    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                    results.append({"index": index, "saved": True, "page_type": page_type, "title": title, "url": url, "txt": str(txt_path), "chars": len(text)})
                except Exception as exc:
                    results.append({"index": index, "saved": False, "page_type": "save_failed", "reason": f"{type(exc).__name__}: {exc}"})
    except Exception as exc:
        return [{
            "index": -1,
            "saved": False,
            "page_type": "cdp_unavailable",
            "reason": f"Cannot connect to Edge CDP at {cdp_endpoint}: {type(exc).__name__}",
        }]
    return results


def human_download_guidance(snapshot: BrowserPageSnapshot) -> list[str]:
    publisher = snapshot.publisher
    page_type = snapshot.page_type
    tips = [f"Current page type: {page_type}; publisher: {publisher}."]
    if page_type in {"cloudflare_or_captcha", "login_or_institution_gate"}:
        tips.append("Use the visible Edge window to pass verification or institution login; do not close the browser.")
    if publisher == "wiley":
        tips.append("Prefer article HTML if visible; otherwise click PDF/ePDF and save to user_fulltexts.")
    elif publisher == "elsevier":
        tips.append("Try ScienceDirect 'View PDF' after institution session is active; if blocked, use OA/repository route.")
    elif publisher == "aip":
        tips.append("AIP may redirect PDF to abstract; use institution sign-in or OA PDF when available.")
    elif publisher == "acs":
        tips.append("Try ACS article page first, then PDF after institution session is active.")
    else:
        tips.append("If the page shows full article text, save HTML/text; if only metadata, keep it as a manual download lead.")
    tips.append("Save downloaded PDF/HTML/XML into user_fulltexts with DOI in the filename when possible.")
    return tips


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Inspect/save legal literature pages from an Edge-CDP session.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_inspect = sub.add_parser("inspect")
    p_inspect.add_argument("--cdp-endpoint", default=DEFAULT_CDP_ENDPOINT)
    p_inspect.add_argument("--json", action="store_true")
    p_save = sub.add_parser("save-open-pages")
    p_save.add_argument("--cdp-endpoint", default=DEFAULT_CDP_ENDPOINT)
    p_save.add_argument("--output-dir", required=True)
    p_save.add_argument("--min-chars", type=int, default=8000)
    p_routes = sub.add_parser("routes")
    p_routes.add_argument("--doi", required=True)
    p_routes.add_argument("--pii", default="")
    args = parser.parse_args()

    if args.cmd == "inspect":
        rows = [asdict(x) for x in inspect_edge_pages(args.cdp_endpoint)]
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for row in rows:
                print(f"[{row['index']}] {row['page_type']} | {row['publisher']} | {row['text_chars']} chars | {row['title'][:90]}")
                print(f"    {row['url']}")
        return 2 if any(row["page_type"] == "cdp_unavailable" for row in rows) else 0
    if args.cmd == "save-open-pages":
        rows = save_open_article_pages(output_dir=args.output_dir, cdp_endpoint=args.cdp_endpoint, min_chars=args.min_chars)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 2 if any(row.get("page_type") == "cdp_unavailable" for row in rows) else 0
    if args.cmd == "routes":
        print(json.dumps(publisher_route_candidates(args.doi, pii=args.pii), ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
