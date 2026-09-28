"""Upgrade-3 legal-source version & span resolution (ticket 011).

One document identity for every consumer:

- ``DocumentResolver.register_document`` records the legal acquisition basis,
  the local/URL source, raw file hash, canonical text hash and parser, and
  assigns a ``version_id``.  A new raw hash for the same document creates a
  NEW version and supersedes (never overwrites) the previous one; derived
  spans of the old version are marked stale through an invalidation event.
- ``resolve_span`` maps a verbatim quote to char offsets in the canonical
  text plus the physical page (from PDF form feeds) and returns a SPAN_INDEX
  entry; resolution failures are recorded, never padded with context text.
- ``resolve_chunk`` looks a (chunk_id, paper_id) pair across ALL registered
  knowledge bases and reports which one served it; a miss is returned as a
  structured miss with the probed KB list (the R6 m3gap break: coverage-stage
  materialisation lived in a supplemental KB while the consumer only probed
  the main KB, and ``sqlite3.OperationalError`` was swallowed into a silent
  ``None``).  Silent fail-open is removed here.
- body_status: complete | partial | parse_failed | identity_conflict |
  unavailable.  Abstract-only, reference tails and login pages are partial,
  never fulltext.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import unicodedata
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote as _uri_quote

DOC_VERSIONS_SCHEMA = "optomind.upgrade3.document_versions.v1"
SPAN_SCHEMA = "optomind.upgrade3.span_index_entry.v1"
RECEIPT_SCHEMA = "optomind.upgrade3.fulltext_receipt.v1"

_WS = re.compile(r"\s+")


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(path: str) -> str:
    with open(path, "rb") as handle:
        return _sha_bytes(handle.read())


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _object_sha256(value: Any) -> str:
    """Hash the deterministic JSON representation of a resolved object."""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return _sha(payload)


def _norm(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text or "").translate(_FOLD_MAP)
    return _WS.sub(" ", folded.lower()).strip()


def _readonly_sqlite_uri(path: str) -> str:
    """Return a sidecar-free immutable SQLite URI for diagnostic reads."""

    resolved = os.path.abspath(os.fspath(path)).replace("\\", "/")
    return f"file:{_uri_quote(resolved, safe='/:')}?mode=ro&immutable=1"


def _owner_row_identity(
    conn: sqlite3.Connection, chunk_id: str, paper_id: str
) -> Dict[str, Any]:
    """Read the optional identity columns an owner row may carry.

    Older knowledge bases only store the text column; newer ones also record the
    DOI and the acquisition depth.  A missing column is reported as empty instead
    of failing the lookup, but it is never invented from the chunk preview.
    """

    columns = {row[1] for row in conn.execute("PRAGMA table_info(text_chunks)")}
    wanted = [name for name in ("doi", "content_depth", "source_kind")
              if name in columns]
    if not wanted:
        return {"owner_doi": "", "owner_content_depth": "", "owner_source_kind": ""}
    row = conn.execute(
        "SELECT %s FROM text_chunks WHERE chunk_id=? AND paper_id=? LIMIT 1"
        % ", ".join(wanted),
        (chunk_id, paper_id),
    ).fetchone()
    values = dict(zip(wanted, row or ()))
    return {
        "owner_doi": str(values.get("doi") or ""),
        "owner_content_depth": str(values.get("content_depth") or ""),
        "owner_source_kind": str(values.get("source_kind") or ""),
    }


class ParseError(Exception):
    pass


class IdentityConflict(Exception):
    pass


def pdf_pages(source_path: str) -> List[str]:
    """Extract physical pages with the existing poppler parser (one page per
    form feed).  Raises ParseError when nothing usable comes out."""
    fd, out = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        proc = subprocess.run(["pdftotext", "-layout", source_path, out],
                              capture_output=True, timeout=120)
        with open(out, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass
    if proc.returncode != 0 or not text.strip():
        raise ParseError(f"pdftotext failed rc={proc.returncode}")
    pages = text.split("\f")
    pages = [p for p in pages if p.strip()]
    if not pages:
        raise ParseError("pdf produced no text")
    return pages


class DocumentResolver:
    def __init__(self, versions_path: str, span_index_path: str):
        self.versions_path = versions_path
        self.span_index_path = span_index_path
        os.makedirs(os.path.dirname(versions_path) or ".", exist_ok=True)
        self.documents: Dict[str, Dict[str, Any]] = {}
        self._canonical_texts: Dict[str, str] = {}
        self.spans: Dict[str, Dict[str, Any]] = {}
        self.events: List[Dict[str, Any]] = []
        self._kb_registry: Dict[str, str] = {}
        self._canonical_status: Dict[str, Dict[str, Any]] = {}
        self._canonical_memo: Dict[str, Dict[str, Any]] = {}
        if os.path.isfile(versions_path):
            with open(versions_path, encoding="utf-8") as handle:
                self.documents = json.load(handle)
        if os.path.isfile(span_index_path):
            with open(span_index_path, encoding="utf-8") as handle:
                self.spans = json.load(handle)

    # ------------------------------------------------ persistence ----
    def _flush(self) -> None:
        for path in (self.versions_path, self.span_index_path):
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        atomic_json(self.versions_path, self.documents)
        atomic_json(self.span_index_path, self.spans)

    def append_event(self, kind: str, detail: Dict[str, Any]) -> None:
        self.events.append({"kind": kind, "detail": detail})

    def _parse_canonical_source(
        self,
        source_path: str,
        parser: str,
        raw: bytes,
    ) -> Tuple[str, List[str]]:
        if str(parser or "").startswith("pdftotext"):
            pages = pdf_pages(source_path)
            return "\n\f\n".join(pages), pages
        canonical = raw.decode("utf-8", errors="replace")
        return canonical, [canonical]

    def _load_current_canonical(self, document_id: str) -> Dict[str, Any]:
        """Reload and verify a persisted document without creating a version."""

        doc = self.documents.get(document_id)
        if not isinstance(doc, dict):
            self._canonical_texts.pop(document_id, None)
            result = {"status": "unavailable", "document": {}}
            self._canonical_status[document_id] = result
            return result
        source_path = str(doc.get("source_path") or "")
        if not source_path or not os.path.isfile(source_path):
            result = {
                "status": "unavailable",
                "document": dict(doc),
                "reason": "source_file_missing",
            }
            self._canonical_texts.pop(document_id, None)
            self._canonical_status[document_id] = result
            return result
        try:
            mtime_ns = os.stat(source_path).st_mtime_ns
        except OSError as exc:
            result = {
                "status": "unavailable",
                "document": dict(doc),
                "reason": f"source_stat_failed:{type(exc).__name__}",
            }
            self._canonical_texts.pop(document_id, None)
            self._canonical_status[document_id] = result
            return result
        # Re-hashing a 20 MB PDF per access is pure overhead inside one attempt.
        # The disk-level check stays exact: a changed mtime invalidates the
        # memo even when the version identity is unchanged.
        memo = self._canonical_memo.get(document_id)
        if (
            isinstance(memo, dict)
            and memo.get("version_id") == doc.get("version_id")
            and memo.get("raw_hash") == doc.get("raw_hash")
            and memo.get("mtime_ns") == mtime_ns
            and isinstance(memo.get("result"), dict)
        ):
            cached = dict(memo["result"])
            self._canonical_texts[document_id] = str(cached.get("canonical_text") or "")
            self._canonical_status[document_id] = cached
            return cached
        try:
            with open(source_path, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            result = {
                "status": "unavailable",
                "document": dict(doc),
                "reason": f"source_read_failed:{type(exc).__name__}",
            }
            self._canonical_texts.pop(document_id, None)
            self._canonical_status[document_id] = result
            return result
        raw_hash = _sha_bytes(raw)
        expected_raw_hash = str(doc.get("raw_hash") or "")
        if not expected_raw_hash or raw_hash != expected_raw_hash:
            result = {
                "status": "stale",
                "document": dict(doc),
                "actual_raw_hash": raw_hash,
                "expected_raw_hash": expected_raw_hash,
                "reason": "source_raw_hash_changed",
            }
            self._canonical_texts.pop(document_id, None)
            self._canonical_status[document_id] = result
            return result
        parser = str(doc.get("parser") or "")
        try:
            canonical, pages = self._parse_canonical_source(
                source_path,
                parser,
                raw,
            )
        except (ParseError, OSError, subprocess.SubprocessError) as exc:
            result = {
                "status": "parse_failed",
                "document": dict(doc),
                "actual_raw_hash": raw_hash,
                "reason": str(exc),
            }
            self._canonical_texts.pop(document_id, None)
            self._canonical_status[document_id] = result
            return result
        canonical_hash = _sha(canonical) if canonical else None
        expected_canonical_hash = str(doc.get("canonical_text_hash") or "")
        if expected_canonical_hash and canonical_hash != expected_canonical_hash:
            result = {
                "status": "stale",
                "document": dict(doc),
                "actual_raw_hash": raw_hash,
                "actual_canonical_text_hash": canonical_hash,
                "expected_canonical_text_hash": expected_canonical_hash,
                "reason": "canonical_text_hash_changed",
            }
            self._canonical_texts.pop(document_id, None)
            self._canonical_status[document_id] = result
            return result
        self._canonical_texts[document_id] = canonical
        result = {
            "status": "current",
            "document": dict(doc),
            "canonical_text": canonical,
            "actual_raw_hash": raw_hash,
            "actual_canonical_text_hash": canonical_hash,
            "parser": parser,
            "pages": len(pages),
        }
        self._canonical_memo[document_id] = {
            "version_id": doc.get("version_id"),
            "raw_hash": doc.get("raw_hash"),
            "mtime_ns": mtime_ns,
            "result": result,
        }
        self._canonical_status[document_id] = result
        return result

    def verify_document_current(self, document_id: str) -> Dict[str, Any]:
        """Verify the persisted document version against its current source."""

        return dict(self._load_current_canonical(str(document_id)))

    # ------------------------------------------------ documents ----
    def register_document(self, *, document_id: str, source_path: str,
                          legal_basis: str, url: str = "",
                          parser: str = "pdftotext-layout") -> Dict[str, Any]:
        if not os.path.isfile(source_path):
            receipt = {
                "schema_version": RECEIPT_SCHEMA,
                "document_id": document_id, "version_id": None,
                "source_path": source_path, "legal_basis": legal_basis, "url": url,
                "raw_hash": None, "canonical_text_hash": None, "parser": parser,
                "body_status": "unavailable", "pages": 0,
                "reason": "source_file_missing",
            }
            return receipt
        with open(source_path, "rb") as handle:
            raw = handle.read()
        raw_hash = _sha_bytes(raw)
        prev = self.documents.get(document_id)
        if (
            isinstance(prev, dict)
            and prev.get("raw_hash") == raw_hash
            and prev.get("parser") == parser
        ):
            # Re-registering an unchanged document is idempotent.  Keep the
            # original version identity rather than manufacturing v2/v3 for
            # the same bytes; source verification still happens on later
            # consumer access through _load_current_canonical.
            try:
                canonical, pages = self._parse_canonical_source(
                    source_path,
                    parser,
                    raw,
                )
            except (ParseError, OSError, subprocess.SubprocessError) as exc:
                self._canonical_status[document_id] = {
                    "status": "parse_failed",
                    "document": dict(prev),
                    "actual_raw_hash": raw_hash,
                    "reason": str(exc),
                }
                self._canonical_texts.pop(document_id, None)
                return dict(prev)
            self._canonical_texts[document_id] = canonical
            self._canonical_status[document_id] = {
                "status": "current" if canonical else prev.get("body_status"),
                "document": dict(prev),
                "canonical_text": canonical,
                "actual_raw_hash": raw_hash,
                "actual_canonical_text_hash": _sha(canonical) if canonical else None,
                "parser": parser,
                "pages": len(pages),
            }
            return dict(prev)
        if prev and prev.get("raw_hash") and prev["raw_hash"] != raw_hash:
            # same identity, new bytes: new version, old spans superseded
            self.append_event("document_version_invalidated", {
                "document_id": document_id,
                "superseded_version": prev["version_id"], "new_raw_hash": raw_hash})
            for span_id, span in list(self.spans.items()):
                if span.get("document_id") == document_id:
                    span["stale"] = True
        body_status = "complete"
        pages: List[str] = []
        canonical = ""
        try:
            canonical, pages = self._parse_canonical_source(
                source_path,
                parser,
                raw,
            )
        except (ParseError, OSError, subprocess.SubprocessError) as exc:
            body_status = "parse_failed"
            canonical = ""
            self.append_event("parse_failed", {"document_id": document_id,
                                               "reason": str(exc)})
        head_txt = _norm(canonical)[:400]
        if len(_norm(canonical)) < 200 and body_status == "complete":
            body_status = "partial"
        elif body_status == "complete" and re.search(r"sign in|log ?in|create account|subscribe to continue", head_txt):
            body_status = "partial"
        elif body_status == "complete" and re.match(r"references", head_txt):
            body_status = "partial"
        version_id = "v1" if not prev else f"v{int(str(prev['version_id'])[1:]) + 1}"
        doc = {
            "schema_version": DOC_VERSIONS_SCHEMA,
            "document_id": document_id,
            "version_id": version_id,
            "source_path": source_path,
            "legal_basis": legal_basis,
            "url": url,
            "parser": parser,
            "raw_hash": raw_hash,
            "canonical_text_hash": _sha(canonical) if canonical else None,
            "body_status": body_status,
            "pages": len(pages),
            "superseded": bool(prev),
        }
        self.documents[document_id] = doc
        self._canonical_texts[document_id] = canonical
        self._canonical_status[document_id] = {
            "status": "current" if canonical else body_status,
            "document": dict(doc),
            "canonical_text": canonical,
            "actual_raw_hash": raw_hash,
            "actual_canonical_text_hash": _sha(canonical) if canonical else None,
            "parser": parser,
            "pages": len(pages),
        }
        self._flush()
        return doc

    # ------------------------------------------------ spans ----
    def canonical_text(self, document_id: str) -> str:
        # Always revalidate on access.  The in-memory cache is a convenience,
        # never evidence that the source bytes are still current.
        self._load_current_canonical(document_id)
        return self._canonical_texts.get(document_id, "")

    def _cached_span_validation_reason(
        self,
        entry: Dict[str, Any],
        document: Dict[str, Any],
        canonical: str,
        quote: str,
    ) -> Optional[str]:
        """Validate a persisted span against the current document bytes."""

        if entry.get("version_id") != document.get("version_id"):
            return "cached_span_version_mismatch"
        if entry.get("source_sha256") != document.get("raw_hash"):
            return "cached_span_source_hash_mismatch"
        cached_canonical_hash = entry.get("canonical_text_hash")
        if cached_canonical_hash and cached_canonical_hash != _sha(canonical):
            return "cached_span_canonical_text_hash_mismatch"
        if entry.get("pdf_page_kind") != "physical":
            return "cached_span_page_kind_mismatch"
        start, end = entry.get("char_start"), entry.get("char_end")
        if (not isinstance(start, int) or not isinstance(end, int)
                or not (0 <= start < end <= len(canonical))):
            return "cached_span_offset_invalid"
        entry_quote = entry.get("quote")
        if not isinstance(entry_quote, str) or _norm(entry_quote) != _norm(quote):
            return "cached_span_quote_mismatch"
        if entry.get("quote_sha256") != _sha(entry_quote):
            return "cached_span_quote_hash_mismatch"
        if _norm(canonical[start:end]) != _norm(quote):
            return "cached_span_slice_mismatch"
        expected_page = canonical[:start].count("\f") + 1
        if entry.get("physical_page") != expected_page:
            return "cached_span_page_mismatch"
        return None

    def resolve_span(self, document_id: str, quote: str,
                     section: str = "") -> Dict[str, Any]:
        doc = self.documents.get(document_id)
        if not doc or doc.get("body_status") not in ("complete", "partial"):
            return {"span_id": None, "resolved": False,
                    "reason": f"document_not_resolvable:{doc and doc.get('body_status')}"}
        verification = self._load_current_canonical(document_id)
        if verification.get("status") != "current":
            return {
                "span_id": None,
                "resolved": False,
                "document_id": document_id,
                "version_id": doc.get("version_id"),
                "reason": f"document_source_{verification.get('status') or 'unavailable'}",
                "detail": verification.get("reason", ""),
            }
        canonical = self.canonical_text(document_id)
        span_id = "span_" + hashlib.sha256(
            f"{document_id}|{quote}".encode("utf-8")).hexdigest()[:24]
        repaired_invalid_reason = None
        if (
            span_id in self.spans
            and not self.spans[span_id].get("stale")
            and self.spans[span_id].get("resolved") is True
        ):
            cached = self.spans[span_id]
            invalid_reason = self._cached_span_validation_reason(
                cached,
                doc,
                canonical,
                quote,
            )
            if invalid_reason is None:
                return dict(cached, reused=True)
            repaired_invalid_reason = invalid_reason
            self.spans[span_id] = dict(
                cached,
                stale=True,
                invalid=True,
                invalid_reason=invalid_reason,
            )
            self._flush()
        n_text = _norm(canonical)
        n_quote = _norm(quote)
        start = n_text.find(n_quote)
        if not n_quote or start < 0:
            entry = {"schema_version": SPAN_SCHEMA, "span_id": span_id,
                     "document_id": document_id, "version_id": doc.get("version_id"),
                     "source_sha256": doc.get("raw_hash"), "resolved": False,
                     "reason": "quote_not_found_in_canonical_text"}
            if repaired_invalid_reason:
                entry.update({"invalid": True,
                              "repaired_invalid_reason": repaired_invalid_reason})
            self.spans[span_id] = entry
            self._flush()
            return entry
        # map char offset -> physical page (form-feed layout of raw canonical)
        loc = locate_span(canonical, quote)
        if loc is None:
            entry = {"schema_version": SPAN_SCHEMA, "span_id": span_id,
                     "document_id": document_id, "version_id": doc.get("version_id"),
                     "source_sha256": doc.get("raw_hash"), "resolved": False,
                     "reason": "quote_not_found_in_canonical_text"}
            if repaired_invalid_reason:
                entry.update({"invalid": True,
                              "repaired_invalid_reason": repaired_invalid_reason})
            self.spans[span_id] = entry
            self._flush()
            return entry
        raw_pos, raw_end = loc
        page = canonical[:raw_pos].count("\f") + 1
        entry = {
            "schema_version": SPAN_SCHEMA,
            "span_id": span_id,
            "document_id": document_id,
            "version_id": doc["version_id"],
            "source_sha256": doc["raw_hash"],
            "canonical_text_hash": _sha(canonical),
            "resolved": True,
            "quote": quote,
            "quote_sha256": _sha(quote),
            "char_start": raw_pos,
            "char_end": raw_end,
            "physical_page": page,
            "pdf_page_kind": "physical",
            "section": section,
        }
        if repaired_invalid_reason:
            entry.update({"repaired_invalid": True,
                          "repaired_invalid_reason": repaired_invalid_reason})
        self.spans[span_id] = entry
        self._flush()
        return entry

    # ------------------------------------------------ KB chunks ----
    def register_kb(self, name: str, path: str) -> None:
        self._kb_registry[name] = path

    def kb_registry(self) -> Dict[str, str]:
        """Return a copy of the registered owner databases (name -> path)."""

        return dict(self._kb_registry)

    def resolve_chunk(self, chunk_id: str, paper_id: str) -> Dict[str, Any]:
        """Deterministic cross-KB chunk lookup with structured misses."""
        probed: List[Dict[str, Any]] = []
        for name, path in self._kb_registry.items():
            if not os.path.isfile(path):
                probed.append({"kb": name, "path": path, "error": "file_missing"})
                continue
            conn = None
            try:
                conn = sqlite3.connect(_readonly_sqlite_uri(path), uri=True)
                row = conn.execute(
                    "SELECT text FROM text_chunks WHERE chunk_id=? AND paper_id=? LIMIT 1",
                    (chunk_id, paper_id)).fetchone()
                owner_identity = _owner_row_identity(conn, chunk_id, paper_id)
            except sqlite3.Error as exc:
                probed.append({"kb": name, "path": path, "error": str(exc)})
                continue
            finally:
                if conn is not None:
                    conn.close()
            if row:
                chunk_text = row[0] if isinstance(row[0], str) else str(row[0] or "")
                try:
                    source_sha256 = _sha_file(path)
                except OSError:
                    source_sha256 = ""
                text_sha256 = _sha(chunk_text)
                return {"resolved": True, "kb": name, "path": path,
                        "source_path": path, "source_sha256": source_sha256,
                        "object_sha256": _object_sha256({
                            "chunk_id": chunk_id,
                            "paper_id": paper_id,
                            "text": chunk_text,
                        }),
                        "chunk_id": chunk_id, "paper_id": paper_id,
                        "text": chunk_text,
                        "text_sha256": text_sha256,
                        "owner_text_sha256": text_sha256,
                        **owner_identity}
            probed.append({"kb": name, "path": path, "found": False})
        # owner mismatch probe: chunk exists under a different paper_id
        for name, path in self._kb_registry.items():
            if not os.path.isfile(path):
                continue
            conn = None
            try:
                conn = sqlite3.connect(_readonly_sqlite_uri(path), uri=True)
                row = conn.execute(
                    "SELECT paper_id, text FROM text_chunks WHERE chunk_id=? LIMIT 1",
                    (chunk_id,)).fetchone()
            except sqlite3.Error:
                continue
            finally:
                if conn is not None:
                    conn.close()
            if row:
                actual_owner = row[0]
                actual_text = row[1] if isinstance(row[1], str) else str(row[1] or "")
                try:
                    source_sha256 = _sha_file(path)
                except OSError:
                    source_sha256 = ""
                return {"resolved": False, "reason": "identity_conflict",
                        "chunk_id": chunk_id, "claimed_paper_id": paper_id,
                        "actual_owner": actual_owner, "kb": name,
                        "path": path, "source_path": path,
                        "source_sha256": source_sha256,
                        "object_sha256": _object_sha256({
                            "chunk_id": chunk_id,
                            "paper_id": str(actual_owner),
                            "text": actual_text,
                        }),
                        "text_sha256": _sha(actual_text)}
        return {"resolved": False, "reason": "chunk_not_found_in_registered_kbs",
                "chunk_id": chunk_id, "claimed_paper_id": paper_id,
                "probed": probed}


_FOLD_MAP = str.maketrans({chr(0x2010): '-', chr(0x2011): '-', chr(0x2012): '-',
                           chr(0x2013): '-', chr(0x2014): '-', chr(0x2212): '-',
                           chr(0x2018): "'", chr(0x2019): "'", chr(0x201C): '"',
                           chr(0x201D): '"', chr(0x00A0): ' '})


def norm_with_map(text: str):
    out=[]; idxs=[]; in_ws=False
    for i, original in enumerate(text or ""):
        folded = unicodedata.normalize("NFKC", original).translate(_FOLD_MAP)
        for character in folded:
            if character.isspace():
                if not in_ws:
                    out.append(" ")
                    idxs.append((i, i + 1))
                    in_ws = True
                else:
                    idxs[-1] = (idxs[-1][0], i + 1)
            else:
                out.append(character.lower())
                idxs.append((i, i + 1))
                in_ws = False
    return "".join(out), idxs


def locate_span(canonical: str, quote: str):
    n_text, idxs = norm_with_map(canonical)
    n_quote = _norm(quote)
    if not n_quote: return None
    pos = n_text.find(n_quote)
    if pos<0: return None
    return idxs[pos][0], idxs[pos+len(n_quote)-1][1]


def _raw_offset(canonical: str, n_quote: str, n_start: int) -> int:
    """Approximate the raw-text offset of the nth normalised character."""
    count = 0
    in_ws = False
    for i, ch in enumerate(canonical):
        if ch.isspace():
            if not in_ws:
                count += 1
            in_ws = True
        else:
            in_ws = False
            count += 1
        if count >= n_start + len(n_quote):
            return i
    return len(canonical)


def atomic_json(path: str, payload: Any) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
