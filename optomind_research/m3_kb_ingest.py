"""M3 KB 回流模块 — 把 M3-real 找到的 OA 候选解析成 text_chunks 并写入 ReviewKnowledgeBase。

闭环流程：
  OA候选 (doi + oa_url)
    → 下载全文 (PDF / HTML, 多路径策略)
    → 解析文本段 (段落切分)
    → INSERT OR REPLACE INTO KB SQLite text_chunks 表
    → 返回 new_chunk_ids
    → 更新 claim.supporting_text_chunk_ids
    → 重算 claim.saturation_score

设计约束：
  - 不依赖复杂的 PDF 解析框架（pypdf 可选，纯文本段落切分兜底）
  - INSERT OR REPLACE 保证幂等，多次运行安全
  - 每条 chunk 带 source_gap_id 溯源字段，可追踪来自哪个 claim 的补证
  - saturation_score 只能上调，不允许下调（保留 LLM 原始语义评估基础）
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import sqlite3
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.academic_backends.openalex_content import fetch_openalex_content, is_openalex_content_url
from optomind_research.runtime.review_quality_contract import source_route_record

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ── 饱和度重算公式 ────────────────────────────────────────────


def recalculate_saturation(
    current_score: float,
    total_chunk_count: int,
    unique_paper_count: int | None = None,
) -> float:
    """Recalculate saturation without mistaking many paragraphs for consensus.

    ``unique_paper_count`` is authoritative.  ``total_chunk_count`` remains as
    a compatibility fallback for older callers, but the live M3 path always
    supplies source diversity.  Saturation never decreases and never reaches
    3.0 from automatic retrieval alone.
    """
    if unique_paper_count is None:
        score_from_evidence = min(2.7, total_chunk_count / 3.0)
    else:
        n = max(0, int(unique_paper_count))
        score_from_evidence = {0: 0.0, 1: 1.0, 2: 1.8, 3: 2.3}.get(n, 2.7)
    return max(float(current_score), score_from_evidence)


def paper_key_from_chunk_id(chunk_id: str) -> str:
    value = str(chunk_id or "")
    if value.startswith("m3gap:"):
        parts = value.split(":")
        return ":".join(parts[:2]) if len(parts) >= 2 else value
    return value.split(":", 1)[0]


def rank_claim_relevant_chunks(
    claim_text: str,
    chunk_ids: list[str],
    paragraphs: list[str],
    *,
    top_k: int = 4,
    missing_evidence_components: list[str] | None = None,
    retrieval_query: str = "",
    audit: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Select claim-focused chunks with lexical and phrase-level evidence.

    The selector remains deterministic and local: first score term overlap,
    then strongly reward exact multi-word phrases and causal language shared
    by the claim/components/query and a paragraph.  ``audit`` receives every
    scored candidate so the caller can explain why the final anchors won.
    """
    stop = {
        "the", "and", "for", "with", "from", "that", "this", "into", "using",
        "review", "paper", "study", "result", "results", "method", "methods",
    }
    query_texts = [str(claim_text or ""), str(retrieval_query or "")]
    query_texts.extend(str(value or "") for value in (missing_evidence_components or []))
    query_text = " ".join(query_texts).lower()
    query_tokens = {
        w for w in re.findall(r"[a-zA-Z]{3,}", query_text)
        if w not in stop
    }
    claim_tokens = {
        w for w in re.findall(r"[a-zA-Z]{3,}", str(claim_text or "").lower())
        if w not in stop
    }
    phrases: list[str] = []
    for text in query_texts:
        words = [w for w in re.findall(r"[a-zA-Z]{3,}", text.lower()) if w not in stop]
        for width in (2, 3, 4, 5):
            phrases.extend(" ".join(words[i:i + width]) for i in range(max(0, len(words) - width + 1)))
    phrases = list(dict.fromkeys(p for p in phrases if len(p) >= 10))
    causal_markers = {
        "cause", "causes", "caused", "causing", "lead", "leads", "led", "reduce",
        "reduces", "reduced", "decrease", "decreases", "mitigate", "mitigates",
        "prevent", "prevents", "because", "therefore", "due", "resulting",
    }
    query_has_causal = bool(causal_markers & set(re.findall(r"[a-zA-Z]{3,}", query_text)))
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for chunk_id, paragraph in zip(chunk_ids, paragraphs):
        normalized_paragraph = compact_text(paragraph, 10000).lower()
        tokens = {w for w in re.findall(r"[a-zA-Z]{3,}", normalized_paragraph) if w not in stop}
        overlap = query_tokens & tokens
        if not overlap:
            continue
        phrase_hits = [phrase for phrase in phrases if phrase in normalized_paragraph]
        paragraph_causal = bool(causal_markers & tokens)
        claim_overlap = len(claim_tokens & tokens)
        score = (
            1.5 * len(overlap) / max(1.0, len(query_tokens) ** 0.5)
            + len(overlap) / max(1.0, len(tokens) ** 0.5)
            + 2.5 * len(phrase_hits)
            + 0.35 * claim_overlap
            + (1.5 if query_has_causal and paragraph_causal else 0.0)
        )
        reasons = []
        if overlap:
            reasons.append(f"term_overlap={len(overlap)}")
        if phrase_hits:
            reasons.append(f"exact_phrases={len(phrase_hits)}")
        if query_has_causal and paragraph_causal:
            reasons.append("causal_language_match")
        detail = {
            "chunk_id": str(chunk_id),
            "score": round(score, 4),
            "matched_terms": sorted(overlap)[:24],
            "matched_phrases": phrase_hits[:12],
            "selection_reasons": reasons,
        }
        scored.append((score, str(chunk_id), detail))
    scored.sort(key=lambda row: (-row[0], row[1]))
    selected = [chunk_id for _, chunk_id, _ in scored[:max(0, top_k)]]
    if audit is not None:
        for rank, (_, chunk_id, detail) in enumerate(scored, start=1):
            detail["rank"] = rank
            detail["selected"] = chunk_id in selected
            audit.append(detail)
    return selected


# ── 数据结构 ──────────────────────────────────────────────────


@dataclass
class IngestResult:
    """KB 回流结果，绑定到原始 claim。"""
    claim_id: str
    new_chunk_ids: list[str] = field(default_factory=list)
    reused_chunk_ids: list[str] = field(default_factory=list)
    new_paper_ids: list[str] = field(default_factory=list)
    candidate_bound_chunk_ids: list[str] = field(default_factory=list)
    factual_candidate_chunk_ids: list[str] = field(default_factory=list)
    method_transfer_chunk_ids: list[str] = field(default_factory=list)
    new_saturation_score: float = 0.0
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "new_chunk_ids": self.new_chunk_ids,
            "reused_chunk_ids": self.reused_chunk_ids,
            "new_paper_ids": self.new_paper_ids,
            "candidate_bound_chunk_ids": self.candidate_bound_chunk_ids,
            "factual_candidate_chunk_ids": self.factual_candidate_chunk_ids,
            "method_transfer_chunk_ids": self.method_transfer_chunk_ids,
            "new_saturation_score": self.new_saturation_score,
            "stats": self.stats,
        }


# ── 文本工具 ──────────────────────────────────────────────────


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_doi(value: Any) -> str:
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(value or "").strip(), flags=re.I)
    text = re.sub(r"^doi:\s*", "", text, flags=re.I).strip().strip(".;,").lower()
    text = re.sub(r"^(10\.\d{4,9})/+", r"\1/", text)
    return text


def doi_to_paper_id(doi: str) -> str:
    d = normalize_doi(doi)
    return f"doi:{d}" if d else f"unknown:{hashlib.sha1(str(doi).encode()).hexdigest()[:12]}"


def doi_to_slug(doi: str) -> str:
    d = normalize_doi(doi)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", d).strip("-")[:60]


def sha1_hex(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()


def safe_task_directory_name(value: Any, fallback: str = "task") -> str:
    """Return a deterministic task directory component valid on Windows."""

    raw = str(value or "").strip()
    readable = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", raw)
    readable = re.sub(r"[. ]+$", "", readable).strip("- .")
    readable = readable[:80] or str(fallback or "task")
    return f"{readable}-{sha1_hex(raw or readable)[:10]}"


def compact_text(value: Any, limit: int = 2000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def normalize_extracted_text(text: Any) -> str:
    """Apply Unicode NFKC normalization before scholarly text search."""
    return unicodedata.normalize("NFKC", str(text or ""))


def _candidate_relevance_tier(candidate: dict[str, Any]) -> str:
    """Return the audited relevance tier accepted by abstract fallback.

    M3's reranker currently calls the strongest tier ``direct``.  The helper
    also accepts the explicit ``partial``/``strongly_relevant`` labels used by
    other candidate-review producers, while deliberately excluding merely
    adjacent/background candidates.
    """
    values: list[Any] = [candidate.get("llm_relevance_grade")]
    for key in ("relevance_review", "candidate_relevance", "relevance_audit", "relevance", "review"):
        value = candidate.get(key)
        if isinstance(value, dict):
            values.extend(value.get(k) for k in ("grade", "tier", "status", "classification", "label", "relevance"))
        else:
            values.append(value)
    # Keep the canonical scope labels end-to-end.  Adjacent/background items are
    # allowed to reach the abstract fallback, but candidate_evidence_policy()
    # prevents them from becoming factual claim support.
    accepted = {
        "direct", "partial", "strong", "strongly relevant", "strongly_relevant",
        "adjacent", "related", "background", "contextual",
    }
    for value in values:
        normalized = re.sub(r"[-_]", " ", str(value or "").strip().lower())
        if normalized in accepted:
            return normalized.replace(" ", "_")
    return ""


def candidate_evidence_policy(candidate: dict[str, Any]) -> dict[str, str | bool]:
    """Normalize retrieval scope into an enforceable downstream use policy.

    Older M3 artifacts predate the explicit scope/role fields.  For those,
    ``adjacent`` evidence is conservatively treated as method transfer rather
    than factual support.  This keeps useful analogies without letting a
    cross-domain paper silently raise a scientific claim's saturation.
    """
    scope = str(candidate.get("llm_scope_fit") or "").strip().lower()
    role = str(candidate.get("llm_retrieval_role") or "").strip().lower()
    grade = str(candidate.get("llm_relevance_grade") or "").strip().lower()
    support = str(candidate.get("llm_support_status") or "").strip().lower()

    valid_scopes = {"in_domain", "cross_domain_analogy", "off_domain"}
    valid_roles = {"evidence_candidate", "method_transfer", "background_only", "reject"}
    if scope not in valid_scopes or role not in valid_roles:
        if grade in {"direct", "strong", "strongly_relevant", "partial"}:
            scope, role = "in_domain", "evidence_candidate"
        elif grade in {"adjacent", "related", "background"}:
            scope, role = "cross_domain_analogy", "method_transfer"
        else:
            scope, role = "off_domain", "reject"

    if scope == "off_domain" or role == "reject":
        role = "reject"
    elif scope == "cross_domain_analogy" and role == "evidence_candidate":
        role = "method_transfer"
    if support in {"contradicts", "not_established", "unsupported"} and role == "evidence_candidate":
        role = "background_only"

    return {
        "scope_fit": scope,
        "retrieval_role": role,
        "factual_support_allowed": role == "evidence_candidate" and scope == "in_domain",
    }


def _candidate_source_ref(candidate: dict[str, Any]) -> str:
    """Return a traceable source reference for candidate provenance."""
    for key in (
        "source_url", "best_url", "oa_url", "pdf_url", "open_access_url",
        "publisher_url", "landing_page_url", "source", "source_id",
    ):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    backends = candidate.get("backends")
    if isinstance(backends, list):
        names = [str(item).strip() for item in backends if str(item).strip()]
        if names:
            return ",".join(names)
    return ""


def _abstract_fallback_reason(candidate: dict[str, Any]) -> str:
    if bool(candidate.get("skip_abstract_fallback")):
        return "abstract_already_managed_by_upstream"
    policy = candidate_evidence_policy(candidate)
    if policy["retrieval_role"] == "reject":
        return "off_domain_or_rejected"
    if not normalize_doi(candidate.get("doi", "")):
        return "missing_doi"
    if not compact_text(candidate.get("title", ""), 400):
        return "missing_title"
    if not _candidate_source_ref(candidate):
        return "missing_traceable_source"
    if not str(candidate.get("abstract") or "").strip():
        return "missing_abstract"
    if not _candidate_relevance_tier(candidate):
        return "weak_or_unreviewed_relevance"
    return ""


def split_paragraphs(text: str, min_chars: int = 80, max_chars: int = 1600) -> list[str]:
    """简单段落切分：按空行分段，过短的相邻段合并，过长的按句切分。"""
    raw_paras = re.split(r"\n{2,}", text)
    paras: list[str] = []
    buf = ""
    for p in raw_paras:
        p = re.sub(r"\s+", " ", p).strip()
        if not p:
            continue
        if len(buf) + len(p) < min_chars:
            buf = (buf + " " + p).strip()
        else:
            if buf:
                paras.append(buf)
            buf = p
    if buf:
        paras.append(buf)

    # 按句子切分过长段落
    result: list[str] = []
    for para in paras:
        if len(para) <= max_chars:
            result.append(para)
        else:
            sentences = re.split(r"(?<=[.!?])\s+", para)
            chunk = ""
            for sent in sentences:
                if len(chunk) + len(sent) + 1 <= max_chars:
                    chunk = (chunk + " " + sent).strip()
                else:
                    if chunk:
                        result.append(chunk)
                    chunk = sent
            if chunk:
                result.append(chunk)
    return [p for p in result if len(p) >= min_chars // 2]


# ── PDF / HTML 下载 ───────────────────────────────────────────

_HEADERS = {
    "User-Agent": "Mozilla/5.0 OptoMind-Research/1.0 (mailto:research@example.com)"
}
_DOWNLOAD_TIMEOUT = 20
_DOWNLOAD_READ_CHUNK_BYTES = 64 * 1024
_DOWNLOAD_TOTAL_DEADLINE_SECONDS = 60
_DOWNLOAD_MAX_PAYLOAD_BYTES = 100 * 1024 * 1024
_SENSITIVE_QUERY_NAMES = {
    "key",
    "api_key",
    "apikey",
    "token",
    "access_token",
    "auth",
    "authorization",
    "signature",
    "sig",
    "credential",
}


def sanitize_audit_url(url: str) -> str:
    """Remove secret-like query values before a URL reaches durable audit."""

    value = str(url or "").strip()
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlsplit(value)
        safe_query = []
        for key, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            if key.casefold() in _SENSITIVE_QUERY_NAMES:
                safe_query.append((key, "[REDACTED]"))
            else:
                safe_query.append((key, item))
        return urllib.parse.urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urllib.parse.urlencode(safe_query),
                parsed.fragment,
            )
        )
    except Exception:
        return value.split("?", 1)[0]


def _sanitize_for_persistence(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).casefold() in _SENSITIVE_QUERY_NAMES:
                clean[str(key)] = "[REDACTED]"
            else:
                clean[str(key)] = _sanitize_for_persistence(item)
        return clean
    if isinstance(value, (list, tuple)):
        return [_sanitize_for_persistence(item) for item in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return sanitize_audit_url(value)
    return value


def _read_response_bounded(
    resp: Any,
    *,
    clock: Any = time.monotonic,
    total_deadline_seconds: float = _DOWNLOAD_TOTAL_DEADLINE_SECONDS,
    max_payload_bytes: int = _DOWNLOAD_MAX_PAYLOAD_BYTES,
    chunk_size: int = _DOWNLOAD_READ_CHUNK_BYTES,
) -> bytes | None:
    """Read a response in bounded chunks under a total wall-clock deadline.

    The socket timeout still bounds any single blocking read, and the
    wall-clock deadline is checked around every chunk so a trickling server
    cannot run indefinitely.  Oversize or deadline expiry returns ``None`` so
    the caller can continue with the next OA route.
    """
    deadline = clock() + float(total_deadline_seconds)
    chunks: list[bytes] = []
    total = 0
    while True:
        if clock() >= deadline:
            return None
        try:
            chunk = resp.read(int(chunk_size))
        except Exception:
            return None
        if not chunk:
            break
        total += len(chunk)
        if total > int(max_payload_bytes):
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _try_download_bytes(
    url: str,
    *,
    clock: Any = time.monotonic,
    total_deadline_seconds: float = _DOWNLOAD_TOTAL_DEADLINE_SECONDS,
    max_payload_bytes: int = _DOWNLOAD_MAX_PAYLOAD_BYTES,
    chunk_size: int = _DOWNLOAD_READ_CHUNK_BYTES,
) -> bytes | None:
    """尝试 HTTP GET 下载，返回 bytes 或 None（失败时静默）。"""
    if not url or not url.startswith("http"):
        return None
    if is_openalex_content_url(url):
        try:
            data, _ = fetch_openalex_content(
                url, timeout=_DOWNLOAD_TIMEOUT, headers=_HEADERS
            )
        except Exception:
            return None
        return data
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            return _read_response_bounded(
                resp,
                clock=clock,
                total_deadline_seconds=total_deadline_seconds,
                max_payload_bytes=max_payload_bytes,
                chunk_size=chunk_size,
            )
    except Exception:
        return None


def _extract_text_from_pdf_bytes(data: bytes) -> str:
    # PyMuPDF first: it generally preserves word spacing better for publisher PDFs.
    try:
        import fitz  # type: ignore

        doc = fitz.open(stream=data, filetype="pdf")
        try:
            parts = [page.get_text() for page in doc]
            text = "\n\n".join(p for p in parts if p.strip())
        finally:
            close = getattr(doc, "close", None)
            if callable(close):
                close()
        if text.strip():
            return normalize_extracted_text(text)
    except Exception:
        pass
    """从 PDF bytes 中提取文本（优先 pypdf，其次 PyMuPDF，最后返回空字符串）。"""
    # 尝试 pypdf
    try:
        import io
        import pypdf  # type: ignore

        reader = pypdf.PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages:
            t = page.extract_text() or ""
            if t.strip():
                parts.append(t)
        return normalize_extracted_text("\n\n".join(parts))
    except Exception:
        pass

    # 尝试 PyMuPDF (fitz)
    try:
        import fitz  # type: ignore

        doc = fitz.open(stream=data, filetype="pdf")
        parts = [page.get_text() for page in doc]
        return "\n\n".join(p for p in parts if p.strip())
    except Exception:
        pass

    return ""


def _extract_text_from_document_bytes(raw: bytes) -> str:
    """Extract usable scholarly text from either PDF or publisher HTML."""
    if raw[:4] == b"%PDF":
        text = _extract_text_from_pdf_bytes(raw)
    else:
        html = raw.decode("utf-8", errors="replace")
        try:
            from bs4 import BeautifulSoup  # type: ignore
            soup = BeautifulSoup(html, "html.parser")
            for bad in soup(["script", "style", "nav", "footer", "noscript", "svg"]):
                bad.decompose()
            text = soup.get_text("\n", strip=True)
        except Exception:
            text = re.sub(r"<[^>]+>", "\n", html)
            text = re.sub(r"&[a-z]+;", " ", text)
        probe = text.lower()
        structure_hits = sum(
            1 for key in ("abstract", "introduction", "methods", "results", "discussion", "references")
            if key in probe
        )
        if len(text) < 3000 or structure_hits < 2:
            return ""
    text = normalize_extracted_text(text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) >= 1000 else ""


def _unusable_download_status(raw: bytes) -> str:
    """Separate a non-article HTML response from a true parse failure."""

    if raw[:4] == b"%PDF":
        return "parse_failed"
    probe = raw[:200_000].decode("utf-8", errors="ignore").casefold()
    page_markers = (
        "<html", "<!doctype html", "sign in", "log in", "access denied",
        "enable javascript", "cookie policy", "article abstract",
    )
    return "not_fulltext_page" if any(marker in probe for marker in page_markers) else "parse_failed"


def _collect_oa_urls(candidate: dict[str, Any]) -> list[str]:
    """从 OA 候选中收集全部可用的 URL，按优先级排序。

    优先级顺序（S2VIP升级后）：
    1. pdf_url (Semantic Scholar openAccessPdf.url, 100 req/sec with API key)
    2. url_for_pdf (OpenAlex primary_location)
    3. oa_url, best_oa_url (Unpaywall)
    4. fulltext_url, html_url, repository_url (fallback)
    """
    urls: list[str] = []
    # S2VIP: pdf_url 优先（Semantic Scholar openAccessPdf direct URL）
    for field in (
        "pdf_url", "url_for_pdf", "open_access_url", "oa_url", "best_oa_url",
        "fulltext_url", "html_url", "repository_url", "source_url", "best_url",
    ):
        v = candidate.get(field)
        if v and isinstance(v, str) and v.startswith("http"):
            urls.append(v)
    extra = (candidate.get("alternate_urls") or []) + (candidate.get("extra_urls") or [])
    for u in extra:
        if u and isinstance(u, str) and u.startswith("http"):
            urls.append(u)
    # 去重，保序
    seen: set[str] = set()
    result: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


def download_and_extract(
    candidate: dict[str, Any],
    download_dir: Path | None = None,
    attempt_audit: list[dict[str, str]] | None = None,
) -> tuple[str, str]:
    """下载 OA 候选并提取文本。返回 (text, source_url)；失败时返回 ('', '')。"""
    local_path = Path(str(candidate.get("local_download_path") or ""))
    if str(local_path) not in {"", "."} and local_path.is_file():
        try:
            text = _extract_text_from_document_bytes(local_path.read_bytes())
            if text:
                if attempt_audit is not None:
                    attempt_audit.append(
                        {"url": str(local_path), "status": "success"}
                    )
                return text, str(local_path)
        except Exception:
            if attempt_audit is not None:
                attempt_audit.append(
                    {"url": str(local_path), "status": "parse_failed"}
                )

    # The section-coverage runtime may already have exhausted and audited its
    # URL waterfall.  Do not silently repeat network attempts that would be
    # absent from attempted_urls/download_errors_by_url.
    if candidate.get("download_attempts_complete"):
        return "", ""

    urls = _collect_oa_urls(candidate)
    if not urls:
        return "", ""

    for url in urls:
        raw = _try_download_bytes(url)
        if not raw:
            if attempt_audit is not None:
                attempt_audit.append(
                    {"url": sanitize_audit_url(url), "status": "download_failed"}
                )
            continue

        # 保存原始文件
        if download_dir:
            download_dir.mkdir(parents=True, exist_ok=True)
            slug = doi_to_slug(candidate.get("doi", "")) or sha1_hex(url)[:12]
            suffix = ".pdf" if raw[:4] == b"%PDF" else ".html"
            (download_dir / f"{slug}{suffix}").write_bytes(raw)

        text = _extract_text_from_document_bytes(raw)
        if text:
            safe_url = sanitize_audit_url(url)
            if attempt_audit is not None:
                attempt_audit.append({"url": safe_url, "status": "success"})
            return text, safe_url
        if attempt_audit is not None:
            attempt_audit.append(
                {
                    "url": sanitize_audit_url(url),
                    "status": _unusable_download_status(raw),
                }
            )

    return "", ""


def _call_download_and_extract(
    candidate: dict[str, Any],
    download_dir: Path | None,
    attempt_audit: list[dict[str, str]],
) -> tuple[str, str]:
    """Call the downloader while preserving legacy two-argument test hooks."""

    try:
        parameters = inspect.signature(download_and_extract).parameters.values()
        accepts_audit = any(
            parameter.name == "attempt_audit"
            or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        accepts_audit = True
    if accepts_audit:
        return download_and_extract(
            candidate, download_dir, attempt_audit=attempt_audit
        )
    return download_and_extract(candidate, download_dir)


# ── SQLite 写入 ───────────────────────────────────────────────


def _ensure_text_chunks_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS text_chunks(
            chunk_id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL DEFAULT '',
            doi TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            ordinal INTEGER,
            section_path TEXT NOT NULL DEFAULT '',
            char_start INTEGER,
            char_end INTEGER,
            char_count INTEGER,
            boilerplate_score REAL NOT NULL DEFAULT 0,
            text TEXT NOT NULL DEFAULT '',
            search_text TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    # These columns are additive so an existing ReviewKnowledgeBase remains
    # readable while evidence provenance is queryable without unpacking JSON.
    existing_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(text_chunks)").fetchall()
    }
    for column, definition in (
        ("evidence_level", "TEXT NOT NULL DEFAULT 'fulltext'"),
        ("source_kind", "TEXT NOT NULL DEFAULT 'fulltext'"),
        ("provenance_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("route_provenance_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("content_depth", "TEXT NOT NULL DEFAULT 'fulltext'"),
        ("use_permission", "TEXT NOT NULL DEFAULT 'contextual_or_qualified_support'"),
        ("context_complete", "INTEGER NOT NULL DEFAULT 1"),
        ("allowed_claim_kinds_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("scope_fit", "TEXT NOT NULL DEFAULT 'unreviewed'"),
        ("relation_roles_json", "TEXT NOT NULL DEFAULT '[]'"),
    ):
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE text_chunks ADD COLUMN {column} {definition}")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS papers(
            paper_id TEXT PRIMARY KEY,
            doi TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            year INTEGER,
            venue TEXT NOT NULL DEFAULT '',
            quality_tier TEXT NOT NULL DEFAULT '',
            query_relevance TEXT NOT NULL DEFAULT '',
            search_text TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS paper_fts USING fts5(paper_id UNINDEXED, title, search_text)")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS text_chunk_fts USING fts5(chunk_id UNINDEXED, paper_id UNINDEXED, title, section_path, text)")


def _upsert_paper(conn: sqlite3.Connection, candidate: dict[str, Any]) -> str:
    persisted_candidate = _sanitize_for_persistence(candidate)
    doi = normalize_doi(candidate.get("doi", ""))
    paper_id = compact_text(candidate.get("paper_id", ""), 300) or doi_to_paper_id(doi)
    title = compact_text(candidate.get("title", ""), 400)
    year = candidate.get("year") or candidate.get("publication_year")
    try:
        year = int(year) if year else None
    except Exception:
        year = None
    search_text = compact_text(
        f"{title} {doi} {candidate.get('abstract', '')}", 800
    )
    existing = conn.execute(
        "SELECT doi,title,year,venue,quality_tier,query_relevance,search_text,raw_json FROM papers WHERE paper_id=?",
        (paper_id,),
    ).fetchone()
    if existing:
        try:
            raw = json.loads(existing[7] or "{}")
            if not isinstance(raw, dict):
                raw = {}
        except Exception:
            raw = {}
        raw.setdefault("m3_real_oa_updates", []).append(persisted_candidate)
        merged = (
            existing[0] or doi,
            existing[1] or title,
            existing[2] or year,
            existing[3] or compact_text(candidate.get("venue", ""), 200),
            existing[4] or "m3_real_oa",
            existing[5] or compact_text(candidate.get("relevance_reason", ""), 300),
            compact_text(f"{existing[6] or ''} {search_text}", 1600),
            json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
        )
        conn.execute(
            "UPDATE papers SET doi=?,title=?,year=?,venue=?,quality_tier=?,query_relevance=?,search_text=?,raw_json=? WHERE paper_id=?",
            (*merged, paper_id),
        )
    else:
        conn.execute(
            """INSERT INTO papers(paper_id,doi,title,year,venue,quality_tier,
               query_relevance,search_text,raw_json)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                paper_id, doi, title, year,
                compact_text(candidate.get("venue", ""), 200),
                "m3_real_oa",
                compact_text(candidate.get("relevance_reason", ""), 300),
                search_text,
                json.dumps(persisted_candidate, ensure_ascii=False, separators=(",", ":")),
            ),
        )
    paper_row = conn.execute("SELECT title,search_text FROM papers WHERE paper_id=?", (paper_id,)).fetchone()
    conn.execute("DELETE FROM paper_fts WHERE paper_id=?", (paper_id,))
    conn.execute(
        "INSERT INTO paper_fts(paper_id,title,search_text) VALUES(?,?,?)",
        (paper_id, paper_row[0] if paper_row else title, paper_row[1] if paper_row else search_text),
    )
    return paper_id


def _upsert_text_chunks(
    conn: sqlite3.Connection,
    paper_id: str,
    candidate: dict[str, Any],
    paragraphs: list[str],
    source_gap_id: str = "",
    source_paragraphs: list[str] | None = None,
    translation_records: list[dict[str, Any]] | None = None,
) -> list[str]:
    doi = normalize_doi(candidate.get("doi", ""))
    title = compact_text(candidate.get("title", ""), 400)
    doi_slug = doi_to_slug(doi) or sha1_hex(paper_id)[:12]
    policy = candidate_evidence_policy(candidate)
    evidence_level = "fulltext" if policy["factual_support_allowed"] else "background"
    source_kind = "fulltext" if policy["factual_support_allowed"] else str(policy["retrieval_role"])
    provenance = {
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "source_gap_id": source_gap_id,
        "scope_fit": policy["scope_fit"],
        "retrieval_role": policy["retrieval_role"],
        "factual_support_allowed": bool(policy["factual_support_allowed"]),
        "relevance_tier": _candidate_relevance_tier(candidate),
    }
    route = source_route_record(
        discovery_route=str(candidate.get("discovery_route") or "m3_oa_search"),
        materialization_route="m3_oa_fulltext_parse",
        content_depth="fulltext",
        scope_fit=str(policy["scope_fit"]),
        context_complete=True,
        events=[{"event": "oa_fulltext_parsed", "source_gap_id": source_gap_id}],
        metadata_conflicts=candidate.get("metadata_conflicts", []),
    )
    provenance["route_provenance"] = route
    chunk_ids: list[str] = []
    char_offset = 0
    for ordinal, text in enumerate(paragraphs):
        source_text = (source_paragraphs or paragraphs)[ordinal] if ordinal < len(source_paragraphs or paragraphs) else text
        translation = (
            translation_records[ordinal]
            if translation_records and ordinal < len(translation_records)
            else {}
        )
        chunk_id = f"m3gap:{doi_slug}:{ordinal:04d}"
        raw = {
            "chunk_id": chunk_id,
            "paper_id": paper_id,
            "doi": doi,
            "title": title,
            "ordinal": ordinal,
            "text": text,
            "source_text": source_text,
            "source_language": translation.get("source_language", "english"),
            "translation_status": translation.get("translation_status", "original_english"),
            "translation_model": translation.get("translation_model", ""),
            "translation_validation_errors": translation.get("validation_errors", []),
            "char_start": char_offset,
            "char_end": char_offset + len(text),
            "char_count": len(text),
            "source_gap_id": source_gap_id,
            "ingest_source": "m3_real_oa",
            "evidence_level": evidence_level,
            "source_kind": source_kind,
            "scope_fit": policy["scope_fit"],
            "retrieval_role": policy["retrieval_role"],
            "factual_support_allowed": bool(policy["factual_support_allowed"]),
            "provenance": provenance,
            "ingested_at": utc_now(),
        }
        conn.execute(
            """INSERT INTO text_chunks(
               chunk_id,paper_id,doi,title,ordinal,section_path,
               char_start,char_end,char_count,boilerplate_score,
               text,search_text,raw_json,evidence_level,source_kind,provenance_json,
               route_provenance_json,content_depth,use_permission,context_complete,
               allowed_claim_kinds_json,scope_fit,relation_roles_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(chunk_id) DO UPDATE SET
                 paper_id=excluded.paper_id, doi=excluded.doi, title=excluded.title,
                 ordinal=excluded.ordinal, section_path=excluded.section_path,
                 char_start=excluded.char_start, char_end=excluded.char_end,
                 char_count=excluded.char_count, text=excluded.text,
                 search_text=excluded.search_text, raw_json=excluded.raw_json,
                 evidence_level=excluded.evidence_level, source_kind=excluded.source_kind,
                 provenance_json=excluded.provenance_json,
                 route_provenance_json=excluded.route_provenance_json,
                 content_depth=excluded.content_depth,
                 use_permission=excluded.use_permission,
                 context_complete=excluded.context_complete,
                 allowed_claim_kinds_json=excluded.allowed_claim_kinds_json,
                 scope_fit=excluded.scope_fit,
                 relation_roles_json=excluded.relation_roles_json""",
            (
                chunk_id, paper_id, doi, title, ordinal,
                "m3_real_ingest",
                char_offset, char_offset + len(text), len(text),
                0.0,
                text,
                compact_text(text, 800),
                json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
                evidence_level,
                source_kind,
                json.dumps(provenance, ensure_ascii=False, separators=(",", ":")),
                json.dumps(route, ensure_ascii=False, separators=(",", ":")),
                route["content_depth"],
                route["use_permission"],
                int(bool(route["context_complete"])),
                json.dumps(route["allowed_claim_kinds"], ensure_ascii=False),
                route["scope_fit"],
                json.dumps([], ensure_ascii=False),
            ),
        )
        conn.execute("DELETE FROM text_chunk_fts WHERE chunk_id=?", (chunk_id,))
        conn.execute(
            "INSERT INTO text_chunk_fts(chunk_id,paper_id,title,section_path,text) VALUES(?,?,?,?,?)",
            (chunk_id, paper_id, title, "m3_real_ingest", text),
        )
        chunk_ids.append(chunk_id)
        char_offset += len(text) + 1
    return chunk_ids


def _paper_has_evidence_kind(
    conn: sqlite3.Connection,
    paper_id: str,
    doi: str,
    source_kind: str,
) -> bool:
    rows = conn.execute(
        """SELECT evidence_level,source_kind,raw_json FROM text_chunks
           WHERE paper_id=? OR (? <> '' AND doi=?)""",
        (paper_id, doi, doi),
    ).fetchall()
    wanted = str(source_kind).lower()
    for evidence_level, existing_kind, raw_json in rows:
        kind = str(existing_kind or evidence_level or "").lower()
        if not kind:
            try:
                raw = json.loads(raw_json or "{}")
                kind = str(raw.get("source_kind") or raw.get("evidence_level") or "fulltext").lower()
            except Exception:
                kind = "fulltext"
        if kind == wanted:
            return True
    return False


def _paper_has_fulltext_chunks(conn: sqlite3.Connection, paper_id: str, doi: str) -> bool:
    rows = conn.execute(
        """SELECT evidence_level,source_kind,raw_json FROM text_chunks
           WHERE paper_id=? OR (? <> '' AND doi=?)""",
        (paper_id, doi, doi),
    ).fetchall()
    for evidence_level, existing_kind, raw_json in rows:
        kind = str(existing_kind or evidence_level or "").lower()
        if not kind:
            try:
                raw = json.loads(raw_json or "{}")
                kind = str(raw.get("source_kind") or raw.get("evidence_level") or "fulltext").lower()
            except Exception:
                kind = "fulltext"
        if kind != "abstract":
            return True
    return False


def _upsert_abstract_chunk(
    conn: sqlite3.Connection,
    paper_id: str,
    candidate: dict[str, Any],
    source_gap_id: str = "",
) -> list[str]:
    """Write one auditable abstract-only chunk, never labelled as fulltext."""
    doi = normalize_doi(candidate.get("doi", ""))
    title = compact_text(candidate.get("title", ""), 400)
    abstract = str(candidate.get("abstract") or "").strip()
    identity_slug = doi_to_slug(doi) or sha1_hex(paper_id)[:16]
    chunk_id = f"m3gap:{identity_slug}:abstract"
    source_ref = _candidate_source_ref(candidate)
    policy = candidate_evidence_policy(candidate)
    evidence_level = "abstract" if policy["factual_support_allowed"] else "background"
    source_kind = "abstract" if policy["factual_support_allowed"] else str(policy["retrieval_role"])
    provenance = {
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "doi": doi,
        "title": title,
        "year": candidate.get("year") or candidate.get("publication_year"),
        "venue": compact_text(candidate.get("venue", ""), 200),
        "source": source_ref,
        "backends": candidate.get("backends") or [],
        "relevance_tier": _candidate_relevance_tier(candidate),
        "source_gap_id": source_gap_id,
        "fulltext_download_succeeded": False,
        "scope_fit": policy["scope_fit"],
        "retrieval_role": policy["retrieval_role"],
        "factual_support_allowed": bool(policy["factual_support_allowed"]),
    }
    route = source_route_record(
        discovery_route=str(candidate.get("discovery_route") or "m3_oa_search"),
        materialization_route="m3_abstract_fallback",
        content_depth="abstract",
        scope_fit=str(policy["scope_fit"]),
        context_complete=False,
        events=[{"event": "abstract_fallback_retained", "source_gap_id": source_gap_id}],
        metadata_conflicts=candidate.get("metadata_conflicts", []),
    )
    provenance["route_provenance"] = route
    raw = {
        "chunk_id": chunk_id,
        "paper_id": paper_id,
        "doi": doi,
        "title": title,
        "year": provenance["year"],
        "venue": provenance["venue"],
        "text": abstract,
        "source_text": abstract,
        "source_language": "english",
        "translation_status": "original_english",
        "source_gap_id": source_gap_id,
        "ingest_source": "m3_real_abstract_fallback",
        "evidence_level": evidence_level,
        "source_kind": source_kind,
        "provenance": provenance,
        "original_abstract": abstract,
        "ingested_at": utc_now(),
    }
    conn.execute(
        """INSERT INTO text_chunks(
           chunk_id,paper_id,doi,title,ordinal,section_path,
           char_start,char_end,char_count,boilerplate_score,
           text,search_text,raw_json,evidence_level,source_kind,provenance_json,
           route_provenance_json,content_depth,use_permission,context_complete,
           allowed_claim_kinds_json,scope_fit,relation_roles_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(chunk_id) DO UPDATE SET
             paper_id=excluded.paper_id, doi=excluded.doi, title=excluded.title,
             text=excluded.text, search_text=excluded.search_text, raw_json=excluded.raw_json,
             evidence_level=excluded.evidence_level, source_kind=excluded.source_kind,
             provenance_json=excluded.provenance_json,
             route_provenance_json=excluded.route_provenance_json,
             content_depth=excluded.content_depth,
             use_permission=excluded.use_permission,
             context_complete=excluded.context_complete,
             allowed_claim_kinds_json=excluded.allowed_claim_kinds_json,
             scope_fit=excluded.scope_fit,
             relation_roles_json=excluded.relation_roles_json""",
        (
            chunk_id, paper_id, doi, title, 0, "Abstract",
            0, len(abstract), len(abstract), 0.0, abstract,
            compact_text(abstract, 800),
            json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
            evidence_level, source_kind, json.dumps(provenance, ensure_ascii=False, separators=(",", ":")),
            json.dumps(route, ensure_ascii=False, separators=(",", ":")),
            route["content_depth"],
            route["use_permission"],
            int(bool(route["context_complete"])),
            json.dumps(route["allowed_claim_kinds"], ensure_ascii=False),
            route["scope_fit"],
            json.dumps([], ensure_ascii=False),
        ),
    )
    conn.execute("DELETE FROM text_chunk_fts WHERE chunk_id=?", (chunk_id,))
    conn.execute(
        "INSERT INTO text_chunk_fts(chunk_id,paper_id,title,section_path,text) VALUES(?,?,?,?,?)",
        (chunk_id, paper_id, title, "Abstract", abstract),
    )
    return [chunk_id]


# ── KBIngester 主类 ───────────────────────────────────────────


class KBIngester:
    """把 M3-real OA 候选回流写入 ReviewKnowledgeBase SQLite。

    用法::

        ingester = KBIngester(kb_sqlite=Path("outputs/review_knowledge_base/.../kb.sqlite"))
        result = ingester.ingest_oa_candidates(selected_candidates, claim=claim_dict)
        # result.new_chunk_ids  → 新写入的 chunk IDs
        # result.new_saturation_score → 重算后的 saturation_score
    """

    def __init__(
        self,
        kb_sqlite: Path | str | None = None,
        download_dir: Path | None = None,
        min_paragraph_chars: int = 80,
        max_paragraph_chars: int = 1600,
        require_scope_audit: bool = False,
    ) -> None:
        self.kb_sqlite = Path(kb_sqlite) if kb_sqlite else None
        self.download_dir = Path(download_dir) if download_dir else None
        self.min_paragraph_chars = min_paragraph_chars
        self.max_paragraph_chars = max_paragraph_chars
        self.require_scope_audit = bool(require_scope_audit)

    def _open_db(self) -> sqlite3.Connection | None:
        if not self.kb_sqlite:
            return None
        self.kb_sqlite.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.kb_sqlite))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _ensure_text_chunks_table(conn)
        return conn

    def ingest_oa_candidates(
        self,
        candidates: list[dict[str, Any]],
        claim: dict[str, Any],
        *,
        download_dir: Path | None = None,
        max_successes: int | None = None,
    ) -> IngestResult:
        """下载 + 解析 OA 候选 → 写入 KB → 返回 IngestResult。

        如果 kb_sqlite 未设置，仍执行下载/解析，但不写 SQLite。
        """
        claim_id = str(claim.get("claim_id", "unknown"))
        source_gap_id = f"m3_gap:{claim.get('section_id', '')}:{claim_id}"
        dl_dir = download_dir or self.download_dir
        if dl_dir:
            dl_dir = Path(dl_dir) / safe_task_directory_name(claim_id, "claim")

        result = IngestResult(claim_id=claim_id)
        stats: dict[str, Any] = {
            "candidates_available": len(candidates),
            "candidates_tried": 0,
            "downloaded": 0,
            "parse_failed": 0,
            "chunks_written": 0,
            "chunks_new": 0,
            "chunks_reused": 0,
            "fulltext_chunks_written": 0,
            "fulltext_papers_materialized": 0,
            "papers_written": 0,
            "reused_local_downloads": 0,
            "translation_required": 0,
            "translated": 0,
            "translation_quarantined": 0,
            "abstract_fallback_candidates": 0,
            "abstract_chunks_written": 0,
            "abstract_fallback_skipped_reasons": {},
            "claim_focused_candidate_fragment_count": 0,
            "claim_focused_scored_fragment_count": 0,
            "claim_focused_final_bound_chunk_ids": [],
            "claim_focused_selection_audit": [],
            "download_attempts": [],
            "scope_policy_distribution": {},
            "rejected_before_download": 0,
            "method_transfer_chunks_written": 0,
        }

        conn = self._open_db()
        from optomind_research.scientific_text_english_normalizer import ScientificTextEnglishNormalizer
        language_normalizer = ScientificTextEnglishNormalizer()
        try:
            for cand in candidates:
                if (
                    max_successes is not None
                    and stats["fulltext_papers_materialized"]
                    >= max(0, int(max_successes))
                ):
                    break
                stats["candidates_tried"] += 1
                if not self.require_scope_audit and not any(
                    str(cand.get(key) or "").strip()
                    for key in ("llm_scope_fit", "llm_retrieval_role", "llm_relevance_grade")
                ):
                    cand = dict(cand)
                    cand["llm_scope_fit"] = "in_domain"
                    cand["llm_retrieval_role"] = "evidence_candidate"
                    cand["scope_audit_status"] = "legacy_unreviewed_allowed_by_caller"
                policy = candidate_evidence_policy(cand)
                policy_key = f"{policy['scope_fit']}:{policy['retrieval_role']}"
                distribution = stats["scope_policy_distribution"]
                distribution[policy_key] = int(distribution.get(policy_key, 0)) + 1
                if policy["retrieval_role"] == "reject":
                    stats["rejected_before_download"] += 1
                    continue
                local_path = Path(str(cand.get("local_download_path") or ""))
                reused_local = str(local_path) not in {"", "."} and local_path.is_file()
                download_attempts: list[dict[str, str]] = []
                text, source_url = _call_download_and_extract(
                    cand, dl_dir, download_attempts
                )
                for attempt in download_attempts:
                    stats["download_attempts"].append(
                        {
                            "candidate_id": str(cand.get("candidate_id") or ""),
                            **attempt,
                        }
                    )
                if not text:
                    stats["parse_failed"] += 1
                    fallback_reason = _abstract_fallback_reason(cand)
                    if fallback_reason:
                        skipped = stats["abstract_fallback_skipped_reasons"]
                        skipped[fallback_reason] = int(skipped.get(fallback_reason, 0)) + 1
                        continue
                    stats["abstract_fallback_candidates"] += 1
                    if conn is None:
                        skipped = stats["abstract_fallback_skipped_reasons"]
                        skipped["kb_unavailable"] = int(skipped.get("kb_unavailable", 0)) + 1
                        continue
                    doi = normalize_doi(cand.get("doi", ""))
                    paper_id = doi_to_paper_id(doi)
                    if _paper_has_fulltext_chunks(conn, paper_id, doi):
                        skipped = stats["abstract_fallback_skipped_reasons"]
                        skipped["existing_fulltext_chunks"] = int(skipped.get("existing_fulltext_chunks", 0)) + 1
                        continue
                    if _paper_has_evidence_kind(conn, paper_id, doi, "abstract"):
                        skipped = stats["abstract_fallback_skipped_reasons"]
                        skipped["abstract_chunk_already_exists"] = int(skipped.get("abstract_chunk_already_exists", 0)) + 1
                        continue
                    selection_audit: list[dict[str, Any]] = []
                    with conn:
                        paper_id = _upsert_paper(conn, cand)
                        existing_ids = {
                            str(row[0]) for row in conn.execute(
                                "SELECT chunk_id FROM text_chunks WHERE paper_id=?", (paper_id,)
                            )
                        }
                        abstract_chunk_ids = _upsert_abstract_chunk(conn, paper_id, cand, source_gap_id)
                    actual_new = [cid for cid in abstract_chunk_ids if cid not in existing_ids]
                    reused = [cid for cid in abstract_chunk_ids if cid in existing_ids]
                    result.new_chunk_ids.extend(actual_new)
                    result.reused_chunk_ids.extend(reused)
                    result.new_paper_ids.append(paper_id)
                    abstract_text = str(cand.get("abstract") or "")
                    retrieval_query = " ".join(
                        str(value or "")
                        for value in (
                            claim.get("retrieval_query"),
                            claim.get("m3_retrieval_query"),
                            claim.get("search_query"),
                        )
                        if str(value or "").strip()
                    )
                    retrieval_query = " ".join(
                        [retrieval_query]
                        + [str(value) for value in (cand.get("query_texts") or []) if str(value).strip()]
                    )
                    bound = rank_claim_relevant_chunks(
                        str(claim.get("statement") or claim.get("claim_text") or ""),
                        abstract_chunk_ids,
                        [abstract_text],
                        top_k=1,
                        missing_evidence_components=list(claim.get("missing_evidence_components") or []),
                        retrieval_query=retrieval_query,
                        audit=selection_audit,
                    )
                    if policy["factual_support_allowed"]:
                        result.factual_candidate_chunk_ids.extend(abstract_chunk_ids)
                        result.candidate_bound_chunk_ids.extend(bound)
                    else:
                        result.method_transfer_chunk_ids.extend(abstract_chunk_ids)
                        stats["method_transfer_chunks_written"] += len(abstract_chunk_ids)
                    stats["chunks_written"] += len(abstract_chunk_ids)
                    stats["chunks_new"] += len(actual_new)
                    stats["chunks_reused"] += len(reused)
                    stats["abstract_chunks_written"] += len(abstract_chunk_ids)
                    stats["papers_written"] += 1
                    stats["claim_focused_candidate_fragment_count"] += len(abstract_chunk_ids)
                    stats["claim_focused_scored_fragment_count"] += len(selection_audit)
                    if policy["factual_support_allowed"]:
                        stats["claim_focused_final_bound_chunk_ids"].extend(bound)
                    stats["claim_focused_selection_audit"].append({
                        "doi": doi,
                        "candidate_id": cand.get("candidate_id", ""),
                        "candidate_fragment_count": len(abstract_chunk_ids),
                        "scored_fragment_count": len(selection_audit),
                        "final_bound_chunk_ids": bound if policy["factual_support_allowed"] else [],
                        "scope_fit": policy["scope_fit"],
                        "retrieval_role": policy["retrieval_role"],
                        "selection_reasons": selection_audit,
                        "evidence_level": "abstract",
                    })
                    continue
                stats["downloaded"] += 1
                if reused_local:
                    stats["reused_local_downloads"] += 1

                text = normalize_extracted_text(text)
                paras = split_paragraphs(
                    text,
                    min_chars=self.min_paragraph_chars,
                    max_chars=self.max_paragraph_chars,
                )
                if not paras:
                    stats["parse_failed"] += 1
                    continue

                normalized_records = language_normalizer.normalize(paras)
                normalized_paras = [record.text_en for record in normalized_records]
                translation_dicts = [record.to_dict() for record in normalized_records]
                stats["translation_required"] += int(language_normalizer.last_audit.get("translation_required") or 0)
                stats["translated"] += int(language_normalizer.last_audit.get("translated") or 0)
                stats["translation_quarantined"] += int(language_normalizer.last_audit.get("quarantined") or 0)

                if conn is not None:
                    selection_audit = []
                    with conn:
                        paper_id = _upsert_paper(conn, cand)
                        existing_ids = {
                            str(row[0]) for row in conn.execute(
                                "SELECT chunk_id FROM text_chunks WHERE paper_id=?", (paper_id,)
                            )
                        }
                        chunk_ids = _upsert_text_chunks(
                            conn,
                            paper_id,
                            cand,
                            normalized_paras,
                            source_gap_id,
                            source_paragraphs=paras,
                            translation_records=translation_dicts,
                        )
                    actual_new = [cid for cid in chunk_ids if cid not in existing_ids]
                    reused = [cid for cid in chunk_ids if cid in existing_ids]
                    result.new_chunk_ids.extend(actual_new)
                    result.reused_chunk_ids.extend(reused)
                    result.new_paper_ids.append(paper_id)
                    selected_bound = rank_claim_relevant_chunks(
                        str(claim.get("statement") or claim.get("claim_text") or ""),
                        chunk_ids,
                        normalized_paras,
                        top_k=4,
                        missing_evidence_components=list(claim.get("missing_evidence_components") or []),
                        retrieval_query=" ".join(
                            [
                                str(value or "")
                                for value in (
                                    claim.get("retrieval_query"),
                                    claim.get("m3_retrieval_query"),
                                    claim.get("search_query"),
                                )
                                if str(value or "").strip()
                            ]
                            + [
                                str(value)
                                for value in (cand.get("query_texts") or [])
                                if str(value).strip()
                            ]
                        ),
                        audit=selection_audit,
                    )
                    if policy["factual_support_allowed"]:
                        result.factual_candidate_chunk_ids.extend(chunk_ids)
                        result.candidate_bound_chunk_ids.extend(selected_bound)
                    else:
                        result.method_transfer_chunk_ids.extend(chunk_ids)
                        stats["method_transfer_chunks_written"] += len(chunk_ids)
                    stats["chunks_written"] += len(chunk_ids)
                    stats["chunks_new"] += len(actual_new)
                    stats["chunks_reused"] += len(reused)
                    stats["fulltext_chunks_written"] += len(chunk_ids)
                    if chunk_ids:
                        stats["fulltext_papers_materialized"] += 1
                    stats["papers_written"] += 1
                    stats["claim_focused_candidate_fragment_count"] += len(chunk_ids)
                    stats["claim_focused_scored_fragment_count"] += len(selection_audit)
                    if policy["factual_support_allowed"]:
                        stats["claim_focused_final_bound_chunk_ids"].extend(selected_bound)
                    stats["claim_focused_selection_audit"].append({
                        "doi": normalize_doi(cand.get("doi", "")),
                        "candidate_id": cand.get("candidate_id", ""),
                        "candidate_fragment_count": len(chunk_ids),
                        "scored_fragment_count": len(selection_audit),
                        "final_bound_chunk_ids": selected_bound if policy["factual_support_allowed"] else [],
                        "scope_fit": policy["scope_fit"],
                        "retrieval_role": policy["retrieval_role"],
                        "selection_reasons": selection_audit,
                        "evidence_level": "fulltext",
                    })
        finally:
            if conn is not None:
                conn.close()

        # 重算 saturation_score
        current_score = float(claim.get("saturation_score", 0.0))
        existing_chunk_count = len(claim.get("supporting_text_chunk_ids") or [])
        # Saturation measures claim-bound evidence, not everything downloaded
        # into the KB.  Background/method-transfer chunks remain discoverable
        # but cannot make a factual claim appear better supported.
        total_chunks = len(set(claim.get("supporting_text_chunk_ids") or [])) + len(
            set(result.candidate_bound_chunk_ids)
        )
        paper_keys = {
            paper_key_from_chunk_id(cid)
            for cid in (claim.get("supporting_text_chunk_ids") or [])
            if paper_key_from_chunk_id(cid)
        }
        paper_keys.update(
            paper_key_from_chunk_id(cid)
            for cid in result.candidate_bound_chunk_ids
            if paper_key_from_chunk_id(cid)
        )
        result.new_saturation_score = recalculate_saturation(
            current_score,
            total_chunks,
            unique_paper_count=len(paper_keys),
        )
        result.stats = stats
        return result
