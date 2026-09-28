"""Deterministic local-first publication metadata resolver and audit.

This module resolves every ``[REF:identity]`` marker used by the latest staged
manuscript into auditable bibliographic metadata without contacting the
network by default.  Resolution precedence is:

1. exact metadata already present locally (unified handoff section files,
   enhanced-section input packets, explanatory citation ledgers, staged
   manuscript context, long-term material caches, and the Semantic Scholar
   response cache);
2. DOI or title-only identities may be enriched through the injectable
   OpenAlex adapter;
3. DOI identities may be enriched through the existing Crossref client;
4. Semantic Scholar paper identities may be enriched through the existing S2
   metadata gateway;
5. a title-only fallback is emitted only with an explicit confidence and
   provenance record.

The resolver never fabricates year/author/venue/DOI and never substitutes
``1900`` as a completeness claim: unresolved fields remain empty and carry a
status/reason.  Basic internal completeness is title + authors + year;
venue/journal and DOI/URL are preferred enrichment targets and their absence
is audited transparently without blocking internal PDF output.  Entries are
deduplicated by canonical DOI, then S2 paper id, then normalized title, while
every alias identity (including every REF marker) is retained in the catalog
so the LaTeX renderer keeps marker mapping intact.

Different references are resolved in bounded parallel work when the
provider-call budget cannot change which references are enriched; otherwise
resolution falls back to the deterministic identity order.  The final catalog
merge is always deterministic and relocation-safe: source paths are stored
project-relative, ordering is stable, and fingerprints depend only on content.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from optomind_research.runtime.openalex_metadata_provider import (
    OpenAlexProvider,
    make_default_openalex_provider,
)


SCHEMA_VERSION = "optomind.publication_metadata_resolver.v1"
CATALOG_FILENAME = "PUBLICATION_METADATA_CATALOG.json"
AUDIT_FILENAME = "PUBLICATION_METADATA_AUDIT.json"

REF_MARKER_PATTERN = re.compile(r"\[REF:([^\]]*)\]")
SECTION_HEADING_PATTERN = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

BIBLIOGRAPHIC_FIELDS = ("title", "authors", "year", "venue", "doi", "url")
REQUIRED_COMPLETENESS_FIELDS = ("title", "authors", "year")

DOI_PATTERN = re.compile(r"^10\.\d{4,9}/[^\s]+$", re.IGNORECASE)
S2_HASH_PATTERN = re.compile(r"^[0-9a-f]{40}$")
HASH_PATTERN = re.compile(r"^[0-9a-f]{8,64}$")
YEAR_PATTERN = re.compile(r"^\d{4}$")
URL_PATTERN = re.compile(r"^https?://\S+$", re.IGNORECASE)

PLACEHOLDER_YEAR = "1900"
PLACEHOLDER_AUTHOR_MARKERS = (
    "authors not recovered",
    "metadata pending",
    "unknown author",
    "n/a",
    "not available",
)
PLACEHOLDER_VENUE_MARKERS = ("metadata pending", "unknown", "n/a", "not available")
REPLACEMENT_CHAR = "\ufffd"
CJK_RANGES = (
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0x3040, 0x30FF),  # Hiragana + Katakana
    (0xAC00, 0xD7AF),  # Hangul syllables
    (0x1100, 0x11FF),  # Hangul Jamo
    (0x3130, 0x318F),  # Hangul Compatibility Jamo
)

IDENTITY_PREFIXES = (
    "doi:",
    "s2:",
    "identity-fallback:",
    "corpusid:",
    "arxiv:",
    "pmid:",
    "hash:",
)

# Field-source trust: local authoritative evidence outranks derived/staged
# data, which outranks provider enrichment (OpenAlex, then Crossref, then
# Semantic Scholar), which outranks title fallback.
SOURCE_TRUST_RANK = {
    "input_packet": 100,
    "explanatory_ledger": 90,
    "supplemental_metadata": 85,
    "staged_context": 80,
    "material_cache": 70,
    "s2_cache": 60,
    "openalex": 55,
    "crossref": 50,
    "s2_provider": 45,
    "title_fallback": 10,
}

CrossrefProvider = Callable[[str], Mapping[str, Any] | None]
S2Provider = Callable[[str], Mapping[str, Any] | None]


class PublicationMetadataError(RuntimeError):
    """Raised when publication metadata cannot be resolved safely."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _project_relative(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PublicationMetadataError(
            f"{label}: cannot read {path}: {exc}"
        ) from exc


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise PublicationMetadataError(
            f"{label}: cannot read/parse {path}: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise PublicationMetadataError(
            f"{label}: expected a JSON object, got "
            f"{type(payload).__name__}: {path}"
        )
    return dict(payload)


def _read_json_value(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise PublicationMetadataError(
            f"{label}: cannot read/parse {path}: {exc}"
        ) from exc


def infer_publication_project_root(
    handoff_path: str | Path,
    *,
    explicit_project_root: str | Path | None = None,
) -> Path:
    """Resolve the root that owns paths stored in a unified handoff.

    Explicit callers remain authoritative.  Portable CLI runs can omit the
    root; in that case the handoff's manifest and section file records are
    used as anchors while walking upward from the handoff directory.
    """

    if explicit_project_root is not None:
        return Path(explicit_project_root).resolve()

    handoff_path = Path(handoff_path).resolve()
    handoff = _read_json_object(handoff_path, "unified handoff")
    relative_paths: list[Path] = []

    manifest_path = str(handoff.get("input_manifest") or "").strip()
    if manifest_path and not Path(manifest_path).is_absolute():
        relative_paths.append(Path(manifest_path))

    sections = handoff.get("sections")
    if isinstance(sections, Mapping):
        for envelope in sections.values():
            if not isinstance(envelope, Mapping):
                continue
            for label in (
                "explanatory_citation_ledger",
                "authoritative_input_packet",
            ):
                record = envelope.get(label)
                if not isinstance(record, Mapping):
                    continue
                raw = str(record.get("path") or "").strip()
                if raw and not Path(raw).is_absolute():
                    relative_paths.append(Path(raw))

    if not relative_paths:
        raise PublicationMetadataError(
            "cannot infer project root: unified handoff has no relative "
            "manifest or section file anchors"
        )

    candidates = [handoff_path.parent, *handoff_path.parents]
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        if all((candidate / relative).is_file() for relative in relative_paths):
            return candidate

    raise PublicationMetadataError(
        "cannot infer project root from unified handoff anchors; pass "
        "--project-root explicitly"
    )


def _empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return not value
    return False


def _field_default(field: str) -> Any:
    return [] if field == "authors" else ""


def _clean_unicode(value: Any) -> str:
    text = str(value or "")
    return unicodedata.normalize("NFKC", text)


def normalize_doi(value: Any) -> str:
    """Normalize a DOI to its canonical lowercase form (no prefix)."""

    text = _clean_unicode(value).strip()
    for prefix in ("https://doi.org/", "http://dx.doi.org/", "doi:", "DOI:"):
        if text.casefold().startswith(prefix.casefold()):
            text = text[len(prefix) :].strip()
    text = re.sub(r"\s+", " ", text)
    return text.casefold()


def normalize_s2_id(value: Any) -> str:
    text = _clean_unicode(value).strip()
    if text.casefold().startswith("s2:"):
        text = text[3:].strip()
    if S2_HASH_PATTERN.fullmatch(text):
        return text.casefold()
    if text.casefold().startswith("corpusid:"):
        return text.casefold()
    return ""


def normalize_title(value: Any) -> str:
    """Deterministic title key for dedupe (NFKC, lowercase, collapsed)."""

    text = _clean_unicode(value).strip().casefold()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w\s]", "", text)
    return text.strip()


def _valid_doi(value: Any) -> str:
    normalized = normalize_doi(value)
    if DOI_PATTERN.fullmatch(normalized):
        return normalized
    return ""


def _valid_year(value: Any) -> tuple[str, str]:
    """Return (year, reject_reason). ``1900`` is never emitted."""

    text = _clean_unicode(value).strip()
    if YEAR_PATTERN.fullmatch(text):
        if text == PLACEHOLDER_YEAR:
            return "", (
                f"year {PLACEHOLDER_YEAR} rejected as an unknown/placeholder "
                "year; never emitted as a completeness claim"
            )
        return text, ""
    match = re.match(r"^(\d{4})[-/]", text)
    if match and match.group(1) != PLACEHOLDER_YEAR:
        return match.group(1), ""
    return "", ""


def _valid_authors(value: Any) -> tuple[list[str], str]:
    if isinstance(value, str):
        raw = [part.strip() for part in re.split(r"\s+and\s+|;", value)]
    else:
        raw = [str(item).strip() for item in (value or [])]
    authors: list[str] = []
    rejected: list[str] = []
    for author in raw:
        if not author:
            continue
        lowered = author.casefold()
        if any(marker in lowered for marker in PLACEHOLDER_AUTHOR_MARKERS):
            rejected.append(author)
            continue
        quality = _metadata_text_quality(author)
        if quality:
            return [], f"corrupt metadata rejected: {quality} (author {author!r})"
        authors.append(author)
    if not authors:
        reason = "no author list recovered"
        if rejected:
            reason += (
                "; placeholder author tokens rejected: "
                + ", ".join(sorted(set(rejected)))
            )
        return [], reason
    return authors, ""


def _valid_venue(value: Any) -> tuple[str, str]:
    text = _clean_unicode(value).strip()
    lowered = text.casefold()
    if not text:
        return "", ""
    if any(marker in lowered for marker in PLACEHOLDER_VENUE_MARKERS):
        return "", f"venue placeholder rejected: {text!r}"
    quality = _metadata_text_quality(text)
    if quality:
        return "", f"corrupt metadata rejected: {quality}"
    return text, ""


def _valid_title(value: Any) -> tuple[str, str]:
    text = _clean_unicode(value).strip()
    if not text:
        return "", ""
    quality = _metadata_text_quality(text)
    if quality:
        return "", f"corrupt metadata rejected: {quality}"
    return text, ""


def _is_cjk_char(char: str) -> bool:
    code = ord(char)
    return any(lower <= code <= upper for lower, upper in CJK_RANGES)


def _cjk_count(text: str) -> int:
    return sum(1 for char in text if _is_cjk_char(char))


def _latin_count(text: str) -> int:
    return sum(1 for char in text if _is_latin_char(char))


def _is_latin_char(char: str) -> bool:
    code = ord(char)
    return (
        0x0041 <= code <= 0x005A  # ASCII uppercase
        or 0x0061 <= code <= 0x007A  # ASCII lowercase
        or 0x00C0 <= code <= 0x024F  # Latin-1 supplement + Latin extended
        or 0x0300 <= code <= 0x036F  # combining diacritical marks
    )


def _is_latin_dominant(text: str) -> bool:
    latin = _latin_count(text)
    cjk = _cjk_count(text)
    return latin > 0 and latin >= cjk


def _cjk_embedded_in_latin(text: str) -> bool:
    """True when a CJK char is adjacent to a Latin letter without a separator.

    Windows mojibake interleaves CJK characters inside otherwise-Latin tokens
    (``End鍦?nd``), while legitimate bilingual names keep CJK as their own
    token (``J. Guo 郭``).  Only the embedded form is treated as corruption.
    """

    chars = list(text)
    for index, char in enumerate(chars):
        if not _is_cjk_char(char):
            continue
        left = chars[index - 1] if index > 0 else ""
        right = chars[index + 1] if index + 1 < len(chars) else ""
        if (left and _is_latin_char(left)) or (right and _is_latin_char(right)):
            return True
    return False


def _metadata_text_quality(text: str) -> str:
    """Return a rejection reason when a metadata string is unusable.

    Conservative mojibake guard for publication metadata: Latin-dominant
    strings that contain replacement characters or CJK sequences are treated
    as corrupt (Windows mojibake), while genuinely predominantly-CJK strings
    survive.  No per-paper titles or replacement mappings are hard-coded.
    """

    if not text:
        return ""
    if REPLACEMENT_CHAR in text and _is_latin_dominant(text):
        return "replacement_character_in_latin_dominant_text"
    cjk = _cjk_count(text)
    latin = _latin_count(text)
    if cjk and latin and latin >= cjk and _cjk_embedded_in_latin(text):
        return "cjk_sequences_in_latin_dominant_text"
    return ""


def _valid_url(value: Any) -> str:
    text = _clean_unicode(value).strip()
    return text if URL_PATTERN.fullmatch(text) else ""


def doi_from_chunk_id(chunk_id: Any) -> str:
    """Recover the DOI embedded in a ``m3gap:<doi-with-/-as-->:<chunk>`` id.

    The material pipeline encodes ``/`` as ``-`` in chunk ids.  Only the first
    separator after the registrar prefix (``10.XXXX``) is a ``/``; genuine
    hyphens in the remainder of the DOI stay intact.  The result is validated
    against the DOI pattern and returned empty when ambiguous/invalid.
    """

    text = _clean_unicode(chunk_id).strip()
    match = re.match(
        r"^(?:m3gap:)?(10\.\d{4,9}-[A-Za-z0-9._()\[\]:;/+-]*?)(?::\d+)?$",
        text,
    )
    if not match:
        return ""
    candidate = match.group(1)
    decoded = re.sub(
        r"^(10\.\d{4,9})-",
        r"\1/",
        candidate,
        count=1,
        flags=re.IGNORECASE,
    )
    return _valid_doi(decoded)


def parse_ref_identity(token: str) -> RefIdentity | None:
    """Parse one ``[REF:...]`` token into a typed identity."""

    raw = _clean_unicode(token).strip()
    if not raw:
        return None
    lowered = raw.casefold()
    for prefix in IDENTITY_PREFIXES:
        if lowered.startswith(prefix):
            value = raw[len(prefix) :].strip()
            if prefix == "doi:":
                value = normalize_doi(value)
            elif prefix in ("s2:", "hash:"):
                value = value.casefold()
            return RefIdentity(
                token=raw,
                kind=prefix.rstrip(":"),
                value=value,
                normalized=f"{prefix.rstrip(':')}:{value}",
            )
    if DOI_PATTERN.fullmatch(raw):
        value = normalize_doi(raw)
        return RefIdentity(
            token=raw, kind="doi", value=value, normalized=f"doi:{value}"
        )
    if S2_HASH_PATTERN.fullmatch(raw):
        value = raw.casefold()
        return RefIdentity(
            token=raw, kind="s2", value=value, normalized=f"s2:{value}"
        )
    if HASH_PATTERN.fullmatch(raw):
        value = raw.casefold()
        return RefIdentity(
            token=raw, kind="hash", value=value, normalized=f"hash:{value}"
        )
    return RefIdentity(
        token=raw, kind="other", value=raw, normalized=f"other:{raw.casefold()}"
    )


class RefIdentity:
    """Typed REF identity with a canonical lookup key."""

    __slots__ = ("token", "kind", "value", "normalized")

    def __init__(self, *, token: str, kind: str, value: str, normalized: str):
        self.token = token
        self.kind = kind
        self.value = value
        self.normalized = normalized

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, RefIdentity)
            and self.normalized == other.normalized
        )

    def __hash__(self) -> int:
        return hash(self.normalized)


def identity_lookup_keys(identity: RefIdentity) -> list[str]:
    """Candidate index keys for one identity (prefix-tolerant)."""

    keys = [identity.normalized]
    if identity.kind == "doi":
        keys.extend(
            [
                identity.value,
                f"https://doi.org/{identity.value}",
                f"http://dx.doi.org/{identity.value}",
            ]
        )
    elif identity.kind == "s2":
        keys.extend([identity.value, f"hash:{identity.value}"])
    elif identity.kind == "hash":
        keys.extend(
            [
                f"identity-fallback:{identity.value}",
                identity.value,
            ]
        )
        if S2_HASH_PATTERN.fullmatch(identity.value):
            keys.append(f"s2:{identity.value}")
    elif identity.kind == "identity-fallback":
        keys.extend(
            [
                f"hash:{identity.value}",
                identity.value,
            ]
        )
    else:
        keys.extend([identity.token, identity.token.casefold()])
    return keys


def marker_occurrences(text: str) -> list[dict[str, Any]]:
    """All REF marker occurrences with stable section attribution."""

    headings = [
        (match.start(), match.group(1).strip())
        for match in SECTION_HEADING_PATTERN.finditer(text)
    ]
    occurrences: list[dict[str, Any]] = []
    for match in REF_MARKER_PATTERN.finditer(text):
        token = match.group(1).strip()
        section = "front_matter"
        for position, heading in headings:
            if position < match.start():
                section = heading
            else:
                break
        occurrences.append(
            {
                "token": token,
                "position": match.start(),
                "section": section,
            }
        )
    return occurrences


def inventory_ref_identities(text: str) -> dict[str, Any]:
    """Inventory unique REF identities, counts, sections, and malformed refs."""

    occurrences = marker_occurrences(text)
    order: list[str] = []
    counts: dict[str, int] = {}
    sections: dict[str, list[str]] = {}
    identities: dict[str, RefIdentity] = {}
    malformed: list[dict[str, Any]] = []
    for occurrence in occurrences:
        token = occurrence["token"]
        identity = parse_ref_identity(token)
        if identity is None:
            malformed.append(
                {
                    "token": token,
                    "position": occurrence["position"],
                    "reason": "empty or malformed REF identity",
                }
            )
            continue
        if token not in order:
            order.append(token)
        counts[token] = counts.get(token, 0) + 1
        sections.setdefault(token, [])
        if occurrence["section"] not in sections[token]:
            sections[token].append(occurrence["section"])
        identities[token] = identity
    return {
        "unique_tokens": order,
        "identities": identities,
        "counts": counts,
        "sections": sections,
        "total_occurrences": len(occurrences),
        "malformed": malformed,
    }


def _record_identity_tokens(
    *,
    paper_id: Any,
    marker_id: Any,
    marker: Any,
    handle: Any,
    doi: Any,
    s2_id: Any,
) -> list[str]:
    tokens: list[str] = []
    for candidate in (marker_id, marker, handle, paper_id, s2_id):
        text = _clean_unicode(candidate).strip()
        if text and text not in tokens:
            tokens.append(text)
    normalized_doi = _valid_doi(doi)
    if normalized_doi:
        for candidate in (f"doi:{normalized_doi}", normalized_doi):
            if candidate not in tokens:
                tokens.append(candidate)
    normalized_s2 = normalize_s2_id(s2_id) if s2_id else ""
    if normalized_s2 and f"s2:{normalized_s2}" not in tokens:
        tokens.append(f"s2:{normalized_s2}")
    return tokens


class LocalMetadataIndex:
    """Local metadata records keyed by identity, DOI, S2 id, and title."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        handoff_path: str | Path,
        staged_context_path: str | Path | None = None,
        material_cache_dirs: Sequence[str | Path] = (),
        scan_material_caches: bool = True,
        max_material_cache_roots: int = 8,
        supplemental_metadata_paths: Sequence[str | Path] = (),
        s2_cache_path: str | Path | None = None,
        include_s2_cache: bool = True,
        verify_digests: bool = True,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.verify_digests = verify_digests
        self.max_material_cache_roots = max(0, int(max_material_cache_roots))
        self.records: list[dict[str, Any]] = []
        self.by_identity: dict[str, list[dict[str, Any]]] = {}
        self.by_title: dict[str, list[dict[str, Any]]] = {}
        self.source_files: list[Path] = []
        self.supplemental_records: list[dict[str, Any]] = []
        self.supplemental_by_identity: dict[str, list[dict[str, Any]]] = {}
        self.supplemental_by_title: dict[str, list[dict[str, Any]]] = {}
        self.supplemental_files: list[Path] = []

        handoff_path = Path(handoff_path)
        if not handoff_path.is_file():
            raise PublicationMetadataError(
                f"unified handoff not found: {handoff_path}"
            )
        self._load_handoff(handoff_path)
        if staged_context_path:
            self._load_staged_context(Path(staged_context_path))
        else:
            discovered = self._discover_staged_context()
            if discovered is not None:
                self._load_staged_context(discovered)
        cache_roots = list(material_cache_dirs)
        if scan_material_caches:
            cache_roots.extend(self._discover_material_cache_roots())
        for root in cache_roots:
            self._load_material_cache_root(Path(root))
        if include_s2_cache:
            self._load_s2_cache(
                Path(s2_cache_path)
                if s2_cache_path
                else self.project_root
                / "database"
                / "s2_cache"
                / "s2_online_cache.sqlite"
            )
        for path in supplemental_metadata_paths:
            self._load_supplemental(Path(path))

    def _resolve_record_path(
        self,
        record: Any,
        section_id: str,
        label: str,
    ) -> Path:
        if not isinstance(record, Mapping) or not str(record.get("path") or ""):
            raise PublicationMetadataError(
                f"{section_id}: handoff {label} record is missing a path"
            )
        raw = str(record["path"])
        path = Path(raw)
        if not path.is_absolute():
            path = self.project_root / path
        path = path.resolve()
        if not path.is_file():
            raise PublicationMetadataError(
                f"{section_id}: handoff {label} file not found: {path}"
            )
        expected = str(record.get("sha256") or "")
        if self.verify_digests and expected:
            actual = _sha256_file(path)
            if actual != expected:
                raise PublicationMetadataError(
                    f"{section_id}: handoff {label} digest mismatch for "
                    f"{path} (expected {expected}, got {actual})"
                )
        return path

    def _load_handoff(self, handoff_path: Path) -> None:
        handoff = _read_json_object(handoff_path, "unified handoff")
        self.source_files.append(handoff_path)
        sections_raw = handoff.get("sections")
        if not isinstance(sections_raw, Mapping):
            raise PublicationMetadataError(
                "unified handoff sections must be an object"
            )
        section_order = handoff.get("section_order")
        if not isinstance(section_order, list) or not section_order:
            section_order = list(sections_raw.keys())
        if not section_order:
            raise PublicationMetadataError(
                "unified handoff contains no sections"
            )
        for section_id in section_order:
            envelope = sections_raw.get(str(section_id))
            if not isinstance(envelope, Mapping):
                raise PublicationMetadataError(
                    f"unified handoff missing section envelope: {section_id}"
                )
            ledger_path = self._resolve_record_path(
                envelope.get("explanatory_citation_ledger"),
                str(section_id),
                "explanatory_citation_ledger",
            )
            packet_path = self._resolve_record_path(
                envelope.get("authoritative_input_packet"),
                str(section_id),
                "authoritative_input_packet",
            )
            self._load_ledger(ledger_path, str(section_id))
            self._load_packet(packet_path, str(section_id))

    def _add_record(self, record: dict[str, Any]) -> None:
        self.records.append(record)
        keys: set[str] = set()
        for token in record["identities"]:
            parsed = parse_ref_identity(token)
            if parsed is not None:
                keys.add(parsed.normalized)
            keys.add(token.casefold())
        if record.get("doi"):
            keys.add(f"doi:{normalize_doi(record['doi'])}")
        if record.get("s2_id"):
            keys.add(f"s2:{normalize_s2_id(record['s2_id'])}")
        for key in keys:
            self.by_identity.setdefault(key, []).append(record)
        title_key = normalize_title(record.get("title") or "")
        if title_key:
            self.by_title.setdefault(title_key, []).append(record)

    def _load_ledger(self, path: Path, section_id: str) -> None:
        data = _read_json_object(path, f"{section_id} explanatory ledger")
        relative = _project_relative(path, self.project_root)
        self.source_files.append(path)
        for raw in data.get("records") or []:
            if not isinstance(raw, Mapping):
                continue
            metadata = raw.get("metadata")
            metadata = metadata if isinstance(metadata, Mapping) else {}
            marker_identity = (
                raw.get("marker_id")
                or raw.get("marker")
                or raw.get("handle")
                or metadata.get("paper_id")
                or metadata.get("doi")
                or ""
            )
            if not marker_identity:
                continue
            title = str(
                metadata.get("title") or metadata.get("source_title") or ""
            ).strip()
            authors_raw = metadata.get("authors") or metadata.get("author") or []
            if isinstance(authors_raw, str):
                authors_raw = [authors_raw]
            year = str(
                metadata.get("year")
                or metadata.get("publication_year")
                or ""
            ).strip()
            venue = str(
                metadata.get("venue")
                or metadata.get("journal")
                or metadata.get("source_title")
                or metadata.get("conference")
                or ""
            ).strip()
            doi = str(metadata.get("doi") or "").strip()
            s2_id = str(
                metadata.get("paper_id") or metadata.get("s2_paper_id") or ""
            ).strip()
            self._add_record(
                {
                    "identities": _record_identity_tokens(
                        paper_id=s2_id,
                        marker_id=raw.get("marker_id"),
                        marker=raw.get("marker"),
                        handle=raw.get("handle"),
                        doi=doi,
                        s2_id=s2_id,
                    ),
                    "doi": _valid_doi(doi),
                    "s2_id": normalize_s2_id(s2_id),
                    "title": title,
                    "authors": [str(a).strip() for a in authors_raw if str(a).strip()],
                    "year": year,
                    "venue": venue,
                    "url": str(metadata.get("url") or "").strip(),
                    "abstract": str(metadata.get("abstract") or ""),
                    "source": "explanatory_ledger",
                    "source_path": relative,
                    "section_id": section_id,
                    "trust_type": str(
                        raw.get("permission") or "background_explanation_only"
                    ),
                    "retrieval_origin": str(raw.get("retrieval_origin") or ""),
                }
            )

    def _load_packet(self, path: Path, section_id: str) -> None:
        data = _read_json_object(path, f"{section_id} input packet")
        relative = _project_relative(path, self.project_root)
        self.source_files.append(path)
        seen: set[tuple[str, str, str]] = set()

        def add_paper_record(
            *,
            paper_id: Any,
            title: Any,
            doi: Any,
            chunk_id: Any,
        ) -> None:
            paper_text = _clean_unicode(paper_id).strip()
            title_text = _clean_unicode(title).strip()
            if not paper_text and not title_text:
                return
            explicit_doi = _valid_doi(doi)
            derived_doi = "" if explicit_doi else doi_from_chunk_id(chunk_id)
            final_doi = explicit_doi or derived_doi
            key = (paper_text, title_text, final_doi)
            if key in seen:
                return
            seen.add(key)
            self._add_record(
                {
                    "identities": _record_identity_tokens(
                        paper_id=paper_text,
                        marker_id="",
                        marker="",
                        handle="",
                        doi=final_doi,
                        s2_id=paper_text,
                    ),
                    "doi": final_doi,
                    "s2_id": normalize_s2_id(paper_text),
                    "title": title_text,
                    "authors": [],
                    "year": "",
                    "venue": "",
                    "url": f"https://doi.org/{final_doi}" if final_doi else "",
                    "abstract": "",
                    "source": "input_packet",
                    "source_path": relative,
                    "section_id": section_id,
                    "trust_type": "core_evidence",
                    "retrieval_origin": (
                        "doi_derived_from_chunk_id" if derived_doi else ""
                    ),
                }
            )

        for entry in data.get("evidence_packets") or []:
            if not isinstance(entry, Mapping):
                continue
            add_paper_record(
                paper_id=entry.get("paper_id"),
                title=entry.get("source_title"),
                doi=entry.get("doi"),
                chunk_id=entry.get("chunk_id"),
            )
        coverage = data.get("literature_coverage")
        if isinstance(coverage, Mapping):
            for source in coverage.get("sources") or []:
                if not isinstance(source, Mapping):
                    continue
                add_paper_record(
                    paper_id=source.get("paper_id"),
                    title=source.get("title"),
                    doi=source.get("doi"),
                    chunk_id=source.get("chunk_id"),
                )

    def _load_staged_context(self, path: Path) -> None:
        if not path.is_file():
            return
        data = _read_json_object(path, "staged context")
        relative = _project_relative(path, self.project_root)
        self.source_files.append(path)
        for inventory in (
            data.get("citation_inventory"),
            data.get("local_background_candidates"),
        ):
            if not isinstance(inventory, list):
                continue
            for item in inventory:
                if not isinstance(item, Mapping):
                    continue
                citation_id = str(item.get("citation_id") or "").strip()
                title = str(item.get("title") or "").strip()
                if not citation_id and not title:
                    continue
                paper_id = str(item.get("paper_id") or "").strip()
                doi = str(item.get("doi") or "").strip()
                authors_raw = item.get("authors") or []
                if isinstance(authors_raw, str):
                    authors_raw = [authors_raw]
                provenance = item.get("provenance")
                provenance = (
                    provenance if isinstance(provenance, Mapping) else {}
                )
                section_id = str(provenance.get("section_id") or "").strip()
                year = str(item.get("year") or item.get("publication_year") or "")
                venue = str(
                    item.get("venue")
                    or item.get("journal")
                    or item.get("source_title")
                    or item.get("conference")
                    or ""
                ).strip()
                self._add_record(
                    {
                        "identities": _record_identity_tokens(
                            paper_id=paper_id or citation_id,
                            marker_id=citation_id,
                            marker="",
                            handle="",
                            doi=doi,
                            s2_id=paper_id,
                        ),
                        "doi": _valid_doi(doi),
                        "s2_id": normalize_s2_id(paper_id),
                        "title": title,
                        "authors": [
                            str(a).strip()
                            for a in authors_raw
                            if str(a).strip()
                        ],
                        "year": str(year).strip(),
                        "venue": venue,
                        "url": (
                            f"https://doi.org/{_valid_doi(doi)}"
                            if _valid_doi(doi)
                            else ""
                        ),
                        "abstract": str(item.get("abstract") or ""),
                        "source": "staged_context",
                        "source_path": relative,
                        "section_id": section_id,
                        "trust_type": str(
                            item.get("trust_type")
                            or item.get("trust")
                            or item.get("permission")
                            or "staged_context"
                        ),
                        "retrieval_origin": str(
                            item.get("retrieval_origin") or ""
                        ),
                    }
                )

    def _discover_staged_context(self) -> Path | None:
        candidates = sorted(
            (
                self.project_root / "outputs"
            ).glob("staged_context_*/STAGED_GLOBAL_INPUTS.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return candidates[0] if candidates else None

    def _discover_material_cache_roots(self) -> list[Path]:
        roots = sorted(
            (self.project_root / "outputs").glob("*/long_term_material_cache"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if self.max_material_cache_roots:
            roots = roots[: self.max_material_cache_roots]
        return roots

    def _latest_snapshot(self, root: Path) -> Path | None:
        snapshots = sorted(
            (snapshot / "MATERIAL_UNITS_FINAL.json")
            for snapshot in root.glob("snapshot-*")
            if (snapshot / "MATERIAL_UNITS_FINAL.json").is_file()
        )
        if not snapshots:
            direct = root / "MATERIAL_UNITS_FINAL.json"
            return direct if direct.is_file() else None
        return snapshots[-1]

    def _load_material_cache_root(self, root: Path) -> None:
        if not root.is_dir():
            return
        units_path = self._latest_snapshot(root)
        if units_path is None:
            return
        try:
            data = json.loads(units_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return  # auxiliary cache: fail-open
        relative = _project_relative(units_path, self.project_root)
        self.source_files.append(units_path)
        units = data.get("units") if isinstance(data, Mapping) else None
        if not isinstance(units, list):
            return
        for unit in units:
            if not isinstance(unit, Mapping):
                continue
            identity = unit.get("identity")
            if not isinstance(identity, Mapping):
                continue
            paper_id = str(identity.get("paper_id") or "").strip()
            title = str(identity.get("title") or "").strip()
            doi = str(identity.get("doi") or "").strip()
            if not (paper_id or title):
                continue
            self._add_record(
                {
                    "identities": _record_identity_tokens(
                        paper_id=paper_id,
                        marker_id="",
                        marker="",
                        handle="",
                        doi=doi,
                        s2_id=paper_id,
                    ),
                    "doi": _valid_doi(doi),
                    "s2_id": normalize_s2_id(paper_id),
                    "title": title,
                    "authors": [],
                    "year": "",
                    "venue": "",
                    "url": f"https://doi.org/{_valid_doi(doi)}" if _valid_doi(doi) else "",
                    "abstract": "",
                    "source": "material_cache",
                    "source_path": relative,
                    "section_id": None,
                    "trust_type": "local_material_cache",
                    "retrieval_origin": "",
                }
            )

    def _load_s2_cache(self, path: Path) -> None:
        if not path.is_file():
            return
        relative = _project_relative(path, self.project_root)
        self.source_files.append(path)
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            return
        try:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT endpoint, params_json, response_json FROM s2_cache"
            ).fetchall()
        except sqlite3.Error:
            return
        finally:
            connection.close()
        seen: set[str] = set()
        for row in rows:
            endpoint = str(row["endpoint"] or "")
            try:
                payload = json.loads(row["response_json"] or "null")
            except (ValueError, TypeError):
                continue
            papers: list[dict[str, Any]] = []
            if endpoint.endswith("/paper/search") or endpoint.endswith(
                "/paper/search/match"
            ):
                if isinstance(payload, Mapping) and isinstance(
                    payload.get("data"), Mapping
                ):
                    papers.append(payload["data"])
                elif isinstance(payload, Mapping):
                    papers.extend(
                        item
                        for item in (payload.get("data") or [])
                        if isinstance(item, Mapping)
                    )
            elif endpoint.endswith("/paper/batch"):
                papers.extend(
                    item for item in payload if isinstance(item, Mapping)
                )
            elif "/citations" in endpoint or "/references" in endpoint:
                key = "citingPaper" if "/citations" in endpoint else "citedPaper"
                for item in (payload.get("data") or []) if isinstance(payload, Mapping) else []:
                    paper = item.get(key) if isinstance(item, Mapping) else None
                    if isinstance(paper, Mapping):
                        papers.append(paper)
            elif isinstance(payload, Mapping) and payload.get("paperId"):
                papers.append(payload)
            for paper in papers:
                paper_id = str(paper.get("paperId") or "").strip()
                if not paper_id or paper_id in seen:
                    continue
                seen.add(paper_id)
                external = paper.get("externalIds") or {}
                external = external if isinstance(external, Mapping) else {}
                doi = str(external.get("DOI") or "").strip()
                authors_raw = paper.get("authors") or []
                authors = [
                    str(a.get("name") or "")
                    for a in authors_raw
                    if isinstance(a, Mapping)
                ]
                authors = [a for a in authors if a.strip()]
                year = paper.get("year")
                venue = str(paper.get("venue") or "").strip()
                self._add_record(
                    {
                        "identities": _record_identity_tokens(
                            paper_id=paper_id,
                            marker_id="",
                            marker="",
                            handle="",
                            doi=doi,
                            s2_id=paper_id,
                        ),
                        "doi": _valid_doi(doi),
                        "s2_id": paper_id.casefold(),
                        "title": str(paper.get("title") or "").strip(),
                        "authors": authors,
                        "year": str(year) if year is not None else "",
                        "venue": venue,
                        "url": (
                            f"https://www.semanticscholar.org/paper/{paper_id}"
                            if paper_id
                            else ""
                        ),
                        "abstract": str(paper.get("abstract") or ""),
                        "source": "s2_cache",
                        "source_path": relative,
                        "section_id": None,
                        "trust_type": "local_s2_cache",
                        "retrieval_origin": endpoint,
                    }
                )

    def _load_supplemental(self, path: Path) -> None:
        """Load one auditable supplemental metadata file (strict provenance)."""

        if not path.is_file():
            raise PublicationMetadataError(
                f"supplemental metadata not found: {path}"
            )
        data = _read_json_value(path, "supplemental metadata")
        if isinstance(data, Mapping):
            raw_records = data.get("records")
            if not isinstance(raw_records, list):
                raise PublicationMetadataError(
                    f"supplemental metadata {path}: expected a 'records' list"
                )
        elif isinstance(data, list):
            raw_records = data
        else:
            raise PublicationMetadataError(
                f"supplemental metadata {path}: expected a JSON object with "
                "'records' or a JSON list"
            )
        relative = _project_relative(path, self.project_root)
        self.source_files.append(path)
        self.supplemental_files.append(path)
        for index, raw in enumerate(raw_records):
            if not isinstance(raw, Mapping):
                raise PublicationMetadataError(
                    f"supplemental metadata {path}: record {index} must be "
                    "an object"
                )
            provenance = raw.get("provenance")
            if not isinstance(provenance, Mapping):
                raise PublicationMetadataError(
                    f"supplemental metadata {path}: record {index} requires "
                    "a provenance object"
                )
            for key in ("source", "source_path_or_url", "reason"):
                if not str(provenance.get(key) or "").strip():
                    raise PublicationMetadataError(
                        f"supplemental metadata {path}: record {index} "
                        f"provenance is missing '{key}'"
                    )
            identities_raw = raw.get("identities")
            if isinstance(identities_raw, str):
                identities_raw = [identities_raw]
            if not isinstance(identities_raw, list):
                identities_raw = []
            single_identity = raw.get("identity")
            if single_identity:
                identities_raw.append(single_identity)
            identities = [
                str(value).strip()
                for value in identities_raw
                if str(value).strip()
            ]
            title = str(raw.get("title") or "").strip()
            if not identities and not title:
                raise PublicationMetadataError(
                    f"supplemental metadata {path}: record {index} needs at "
                    "least one identity or a title"
                )
            authors_raw = raw.get("authors") or raw.get("author") or []
            if isinstance(authors_raw, str):
                authors_raw = [authors_raw]
            record = {
                "identities": identities,
                "doi": str(raw.get("doi") or "").strip(),
                "s2_id": str(
                    raw.get("s2_id") or raw.get("paper_id") or ""
                ).strip(),
                "title": title,
                "authors": [
                    str(author).strip()
                    for author in authors_raw
                    if str(author).strip()
                ],
                "year": str(raw.get("year") or raw.get("publication_year") or ""),
                "venue": str(raw.get("venue") or "").strip(),
                "url": str(raw.get("url") or "").strip(),
                "source": "supplemental_metadata",
                "source_path": relative,
                "section_id": None,
                "trust_type": "supplemental_metadata",
                "retrieval_origin": "supplemental_metadata",
                "provenance": {
                    "source": str(provenance.get("source") or ""),
                    "source_path_or_url": str(
                        provenance.get("source_path_or_url") or ""
                    ),
                    "reason": str(provenance.get("reason") or ""),
                },
            }
            self.supplemental_records.append(record)
            keys: set[str] = set()
            for token in identities:
                parsed = parse_ref_identity(token)
                if parsed is not None:
                    keys.add(parsed.normalized)
                keys.add(token.casefold())
            if _valid_doi(record["doi"]):
                normalized = _valid_doi(record["doi"])
                keys.add(f"doi:{normalized}")
                keys.add(normalized)
            if normalize_s2_id(record["s2_id"]):
                keys.add(f"s2:{normalize_s2_id(record['s2_id'])}")
            for key in keys:
                self.supplemental_by_identity.setdefault(key, []).append(record)
            title_key = normalize_title(title)
            if title_key:
                self.supplemental_by_title.setdefault(title_key, []).append(record)

    def find(self, identity: RefIdentity) -> list[dict[str, Any]]:
        """Exact identity-keyed local records for one REF identity."""

        records: list[dict[str, Any]] = []
        seen: set[int] = set()
        for key in identity_lookup_keys(identity):
            for record in self.by_identity.get(key, []):
                marker = id(record)
                if marker not in seen:
                    seen.add(marker)
                    records.append(record)
        return records

    def find_supplemental(self, identity: RefIdentity) -> list[dict[str, Any]]:
        """Exact identity-keyed supplemental records for one REF identity."""

        records: list[dict[str, Any]] = []
        seen: set[int] = set()
        for key in identity_lookup_keys(identity):
            for record in self.supplemental_by_identity.get(key, []):
                marker = id(record)
                if marker not in seen:
                    seen.add(marker)
                    records.append(record)
        return records

    def find_by_title(self, title: Any) -> list[dict[str, Any]]:
        """Records from any local source that match the normalized title."""
        key = normalize_title(title)
        return list(self.by_title.get(key, [])) if key else []

    def find_supplemental_by_title(self, title: Any) -> list[dict[str, Any]]:
        key = normalize_title(title)
        return list(self.supplemental_by_title.get(key, [])) if key else []

    def source_file_records(self) -> list[dict[str, str]]:
        unique: dict[str, str] = {}
        for path in self.source_files:
            relative = _project_relative(path, self.project_root)
            if path.stat().st_size <= 8 * 1024 * 1024:
                unique[relative] = _sha256_file(path)
            else:
                unique[relative] = (
                    f"content:{path.stat().st_size}:large_file_fingerprinted_via_catalog"
                )
        return sorted(
            ({"path": path, "sha256": digest} for path, digest in unique.items()),
            key=lambda item: item["path"],
        )


class ResolverOptions:
    """Injectable provider wiring and safety knobs for the resolver.

    ``allow_openalex`` defaults to ``False`` so the offline/local-first
    behavior is unchanged.  ``max_workers`` bounds reference-level parallelism;
    the resolver automatically falls back to deterministic sequential order
    whenever the provider-call budget could change per-reference outcomes.
    """

    __slots__ = (
        "allow_openalex",
        "allow_crossref",
        "allow_s2",
        "max_provider_calls",
        "openalex_provider",
        "crossref_provider",
        "s2_provider",
        "max_workers",
    )

    def __init__(
        self,
        *,
        allow_openalex: bool = False,
        allow_crossref: bool = False,
        allow_s2: bool = False,
        max_provider_calls: int = 0,
        openalex_provider: OpenAlexProvider | None = None,
        crossref_provider: CrossrefProvider | None = None,
        s2_provider: S2Provider | None = None,
        max_workers: int = 4,
    ) -> None:
        self.allow_openalex = bool(allow_openalex)
        self.allow_crossref = bool(allow_crossref)
        self.allow_s2 = bool(allow_s2)
        self.max_provider_calls = max(0, int(max_provider_calls))
        self.openalex_provider = openalex_provider
        self.crossref_provider = crossref_provider
        self.s2_provider = s2_provider
        self.max_workers = max(1, int(max_workers))


def make_default_crossref_provider() -> CrossrefProvider:
    """Real Crossref lookup via the existing backend (lazy import)."""

    def lookup(doi: str) -> dict[str, Any] | None:
        from tools.academic_backends.crossref_backend import CrossrefBackend

        row = CrossrefBackend(rate_limit=0.25).verify_doi(doi)
        if not row:
            return None
        authors = [str(a).strip() for a in (row.get("authors") or [])]
        return {
            "title": str(row.get("title") or "").strip(),
            "authors": [a for a in authors if a],
            "year": str(row.get("year") or "").strip(),
            "venue": str(
                row.get("journal_or_venue") or row.get("venue") or ""
            ).strip(),
            "doi": str(row.get("doi") or doi).strip(),
            "url": str(
                row.get("url_or_doi")
                or row.get("source_url")
                or f"https://doi.org/{doi}"
            ).strip(),
        }

    return lookup


def make_default_s2_provider() -> S2Provider:
    """Real Semantic Scholar lookup via the existing gateway (lazy import)."""

    def lookup(paper_id: str) -> dict[str, Any] | None:
        from optomind_research.s2_intelligence_gateway import (
            S2IntelligenceGateway,
        )

        record, _response = S2IntelligenceGateway().get_paper(paper_id)
        if record is None:
            return None
        return {
            "title": str(record.title or "").strip(),
            "authors": [str(a).strip() for a in (record.authors or [])],
            "year": str(record.year or "").strip(),
            "venue": str(record.venue or "").strip(),
            "doi": str(record.doi or "").strip(),
            "url": (
                f"https://www.semanticscholar.org/paper/{record.paper_id}"
                if record.paper_id
                else ""
            ),
            "s2_id": str(record.paper_id or paper_id).strip(),
        }

    return lookup


class PublicationMetadataResolver:
    """Resolve, dedupe, and audit REF identities into a bibliography catalog."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        index: LocalMetadataIndex,
        options: ResolverOptions | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.index = index
        self.options = options or ResolverOptions()
        self.provider_calls = {"openalex": 0, "crossref": 0, "s2": 0}
        self.provider_successes = {"openalex": 0, "crossref": 0, "s2": 0}
        self.provider_errors = {"openalex": 0, "crossref": 0, "s2": 0}
        self.placeholder_year_rejections = 0
        self.corrupt_field_rejections = {
            "title": 0,
            "authors": 0,
            "venue": 0,
        }
        self._stats_lock = threading.Lock()

    def resolve(self, manuscript_text: str) -> dict[str, Any]:
        inventory = inventory_ref_identities(manuscript_text)
        tasks = [
            (
                token,
                inventory["identities"][token],
                inventory["counts"][token],
                inventory["sections"][token],
            )
            for token in inventory["unique_tokens"]
        ]
        if self._should_process_in_parallel(len(tasks)):
            with ThreadPoolExecutor(
                max_workers=max(1, int(self.options.max_workers))
            ) as executor:
                entries = list(executor.map(self._resolve_identity_task, tasks))
        else:
            entries = [self._resolve_identity_task(task) for task in tasks]
        entries = self._dedupe_entries(entries)
        audit = self._build_audit(entries, inventory)
        records = self._renderer_records(entries)
        return {
            "schema_version": SCHEMA_VERSION,
            "entries": entries,
            "records": records,
            "audit": audit,
            "malformed_refs": inventory["malformed"],
        }

    def _should_process_in_parallel(self, task_count: int) -> bool:
        """Choose bounded parallelism only when it cannot alter outcomes.

        Reference completion order never changes the final merge, but a
        finite global provider-call budget can change which references get
        enriched when workers race to claim calls.  Each reference needs at
        most three lookups (OpenAlex, Crossref, S2), so parallelism is safe
        only when the budget is unlimited or cannot be exhausted by every
        reference.  Binding budgets fall back to deterministic identity order.
        """

        if self.options.max_workers <= 1 or task_count <= 1:
            return False
        if self.options.max_provider_calls <= 0:
            return True
        return self.options.max_provider_calls >= task_count * 3

    def _resolve_identity_task(
        self,
        task: tuple[str, RefIdentity, int, list[str]],
    ) -> dict[str, Any]:
        token, identity, marker_count, sections = task
        return self._resolve_identity(token, identity, marker_count, sections)

    def _resolve_identity(
        self,
        token: str,
        identity: RefIdentity,
        marker_count: int,
        sections: list[str],
    ) -> dict[str, Any]:
        local_records = self.index.find(identity)
        candidates: list[dict[str, Any]] = []
        title_only_records: list[Mapping[str, Any]] = []
        for record in local_records:
            candidate = self._local_candidate(record, identity)
            corroborated = any(
                not _empty(candidate["fields"].get(field))
                for field in ("doi", "s2_id", "year", "venue", "authors")
            )
            record_source = str(record.get("source") or "")
            if (
                candidate["fields"].get("title")
                and not corroborated
                and record_source in ("staged_context", "material_cache")
            ):
                title_only_records.append(record)
            else:
                candidates.append(candidate)

        # Auditable supplemental metadata: identity match first.
        for record in self.index.find_supplemental(identity):
            candidates.append(
                self._supplemental_candidate(record, identity, match="identity")
            )
        # Title match only when the entry currently has a title-only record
        # with no DOI/S2 corroboration (never a fuzzy global title join).
        if title_only_records:
            for record in title_only_records:
                title = _clean_unicode(record.get("title") or "").strip()
                if not title:
                    continue
                for supplemental in self.index.find_supplemental_by_title(title):
                    candidates.append(
                        self._supplemental_candidate(
                            supplemental,
                            identity,
                            match="title",
                        )
                    )

        candidates = self._dedupe_candidates(candidates)
        best = self._best_candidate(candidates)
        canonical_doi = ""
        canonical_s2 = ""
        if best is not None:
            canonical_doi = _valid_doi(best["fields"].get("doi") or "")
            canonical_s2 = normalize_s2_id(best["fields"].get("s2_id") or "")
        if not canonical_doi and identity.kind == "doi":
            canonical_doi = identity.value
        if not canonical_s2 and identity.kind in ("s2", "hash"):
            candidate_s2 = identity.value if identity.kind == "s2" else ""
            if candidate_s2 and S2_HASH_PATTERN.fullmatch(candidate_s2):
                canonical_s2 = candidate_s2
        elif not canonical_s2 and identity.kind == "corpusid":
            canonical_s2 = identity.value

        canonical_title = _clean_unicode(
            self._merge_fields(candidates).get("title") or ""
        ).strip()
        if not canonical_title:
            for record in title_only_records:
                title, _reason = _valid_title(record.get("title"))
                if title:
                    canonical_title = title
                    break

        self._enrich(candidates, canonical_doi, canonical_s2, canonical_title)

        # Local-index title lookup: a targeted recovery for identities that
        # resolved to title-only records from input_packet (no DOI, no S2 id,
        # no authors/year). This finds s2_cache or ledger records keyed under
        # a different locator for the same paper. Only fires when:
        # 1. The identity itself has no strong locator (not doi:, not s2:)
        # 2. Authors or year are still missing after provider enrichment
        # 3. A title is present to search on
        # This gate prevents merging unrelated entries via title collision and
        # avoids recounting placeholder-year rejections for records already
        # validated in the main identity lookup.
        if canonical_title and identity.kind == "other":
            _merged_now = self._merge_fields(candidates)
            if _empty(_merged_now.get("authors")) or _empty(_merged_now.get("year")):
                # Track which records we already validated in the main pass to
                # avoid double-counting placeholder-year rejections.
                _seen_records: set[int] = {id(rec) for rec in local_records}
                for _title_rec in self.index.find_by_title(canonical_title):
                    if id(_title_rec) in _seen_records:
                        continue  # already validated in main identity lookup
                    _fields, _ = self._validated_fields(_title_rec)
                    if _empty(_fields.get("authors")) and _empty(_fields.get("year")):
                        continue  # record contributes nothing new
                    candidates.append(self._local_candidate(_title_rec, identity))
                candidates = self._dedupe_candidates(candidates)

        # Title-only fallback with explicit confidence/provenance.
        if not any(not _empty(candidate["fields"].get("title")) for candidate in candidates):
            for record in title_only_records:
                candidate = self._local_candidate(record, identity)
                candidate["source"] = "title_fallback"
                candidate["base_source"] = str(record.get("source") or "")
                candidate["confidence"] = "low"
                candidate["exact"] = False
                candidate["reason"] = (
                    "title recovered from a local title-only record keyed by "
                    "exact identity; no DOI/S2/author/year/venue corroboration"
                )
                candidates.append(candidate)
        candidates = self._dedupe_candidates(candidates)

        entry = self._merge_entry(candidates, identity, marker_count, sections)
        return entry

    def _local_candidate(
        self,
        record: Mapping[str, Any],
        identity: RefIdentity,
    ) -> dict[str, Any]:
        fields, missing_reasons = self._validated_fields(record)
        trust_type = str(record.get("trust_type") or "")
        exact = self._record_matches_identity(record, identity)
        s2_id = fields["s2_id"]
        doi = fields["doi"]
        confidence = "high"
        if exact and not s2_id and not doi:
            confidence = "low"
        elif not exact:
            confidence = "medium"
        return {
            "source": str(record.get("source") or "local"),
            "base_source": str(record.get("source") or "local"),
            "source_path": str(record.get("source_path") or ""),
            "section_id": record.get("section_id"),
            "trust_type": trust_type,
            "retrieval_origin": str(record.get("retrieval_origin") or ""),
            "exact": exact,
            "confidence": confidence,
            "reason": (
                "exact identity match in local metadata"
                if exact
                else "local metadata record linked by normalized identity"
            ),
            "fields": fields,
            "missing_reasons": missing_reasons,
            "aliases": list(record.get("identities") or []),
        }

    def _supplemental_candidate(
        self,
        record: Mapping[str, Any],
        identity: RefIdentity,
        *,
        match: str,
    ) -> dict[str, Any]:
        """One supplemental record as a candidate (identity or title match)."""

        fields, missing_reasons = self._validated_fields(record)
        provenance = record.get("provenance") or {}
        return {
            "source": "supplemental_metadata",
            "base_source": str(provenance.get("source") or ""),
            "source_path": str(
                provenance.get("source_path_or_url")
                or record.get("source_path")
                or ""
            ),
            "section_id": record.get("section_id"),
            "trust_type": "supplemental_metadata",
            "retrieval_origin": "supplemental_metadata",
            "exact": match == "identity",
            "confidence": "high" if match == "identity" else "medium",
            "reason": (
                f"supplemental metadata: {provenance.get('reason') or ''} "
                f"(match: {match})"
            ).strip(),
            "fields": fields,
            "missing_reasons": missing_reasons,
            "aliases": list(record.get("identities") or [])
            + ([f"doi:{fields['doi']}"] if fields["doi"] else [])
            + ([f"s2:{fields['s2_id']}"] if fields["s2_id"] else []),
        }

    def _validated_fields(
        self, record: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Validate one record's bibliographic fields (shared quality path)."""

        year, year_reason = _valid_year(record.get("year"))
        if year == "" and str(record.get("year") or "").strip() == PLACEHOLDER_YEAR:
            with self._stats_lock:
                self.placeholder_year_rejections += 1
        authors, author_reason = _valid_authors(record.get("authors"))
        venue, venue_reason = _valid_venue(record.get("venue"))
        title, title_reason = _valid_title(record.get("title"))
        doi = _valid_doi(record.get("doi"))
        url = _valid_url(record.get("url"))
        s2_id = normalize_s2_id(record.get("s2_id") or record.get("paper_id") or "")
        self._count_corrupt_rejections(
            (
                ("title", title_reason),
                ("authors", author_reason),
                ("venue", venue_reason),
            )
        )
        return (
            {
                "title": title,
                "authors": authors,
                "year": year,
                "venue": venue,
                "doi": doi,
                "url": url,
                "s2_id": s2_id,
            },
            {
                "title": title_reason,
                "year": year_reason,
                "authors": author_reason,
                "venue": venue_reason,
            },
        )

    def _count_corrupt_rejections(
        self, field_reasons: Sequence[tuple[str, str]]
    ) -> None:
        for field, reason in field_reasons:
            if reason.startswith("corrupt metadata rejected"):
                with self._stats_lock:
                    self.corrupt_field_rejections[field] = (
                        self.corrupt_field_rejections.get(field, 0) + 1
                    )

    def _record_matches_identity(
        self,
        record: Mapping[str, Any],
        identity: RefIdentity,
    ) -> bool:
        for key in identity_lookup_keys(identity):
            if key in self.index.by_identity and record in self.index.by_identity[key]:
                return True
        return False

    def _dedupe_candidates(
        self, candidates: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        by_key: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            key = (
                candidate.get("source"),
                candidate.get("source_path"),
                candidate.get("section_id"),
                canonical_json(candidate.get("fields")),
            )
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = dict(candidate)
        return sorted(
            by_key.values(),
            key=lambda candidate: (
                self._candidate_rank(candidate),
                str(candidate.get("source") or ""),
                str(candidate.get("source_path") or ""),
            ),
            reverse=True,
        )

    def _best_candidate(
        self, candidates: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any] | None:
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda candidate: self._candidate_rank(candidate),
        )

    def _candidate_rank(self, candidate: Mapping[str, Any]) -> int:
        source_rank = SOURCE_TRUST_RANK.get(
            str(candidate.get("source") or ""), 0
        )
        trust_bonus = 5 if str(candidate.get("trust_type") or "") == "core_evidence" else 0
        exact_bonus = 3 if candidate.get("exact") else 0
        return source_rank * 100 + trust_bonus * 10 + exact_bonus

    def _enrich(
        self,
        candidates: list[dict[str, Any]],
        canonical_doi: str,
        canonical_s2: str,
        canonical_title: str,
    ) -> None:
        missing = self._publication_info_missing(candidates)
        if not missing:
            return

        openalex_kind = ""
        openalex_value = ""
        if self.options.allow_openalex:
            if canonical_doi:
                openalex_kind = "doi"
                openalex_value = canonical_doi
            elif canonical_title and not canonical_s2:
                openalex_kind = "title"
                openalex_value = canonical_title
        if openalex_kind and self._acquire_provider_call("openalex"):
            try:
                raw = (
                    self.options.openalex_provider
                    or make_default_openalex_provider()
                )({"kind": openalex_kind, "value": openalex_value})
            except Exception as exc:
                self._record_provider_error("openalex")
                candidates.append(
                    self._provider_candidate(
                        "openalex",
                        {},
                        canonical_doi=canonical_doi,
                        canonical_s2=canonical_s2,
                        lookup_kind=openalex_kind,
                        reason=(
                            f"OpenAlex {openalex_kind} lookup failed: {exc}"
                        ),
                    )
                )
            else:
                if raw:
                    self._record_provider_success("openalex")
                    reason = (
                        "DOI identity enriched via OpenAlex"
                        if openalex_kind == "doi"
                        else "title-only identity enriched via OpenAlex title search"
                    )
                    candidates.append(
                        self._provider_candidate(
                            "openalex",
                            raw,
                            canonical_doi=canonical_doi,
                            canonical_s2=canonical_s2,
                            lookup_kind=openalex_kind,
                            reason=reason,
                        )
                    )

        if (
            canonical_doi
            and self.options.allow_crossref
            and self._acquire_provider_call("crossref")
        ):
            try:
                raw = (
                    self.options.crossref_provider
                    or make_default_crossref_provider()
                )(canonical_doi)
            except Exception as exc:
                self._record_provider_error("crossref")
                candidates.append(
                    self._provider_candidate(
                        "crossref",
                        {},
                        canonical_doi=canonical_doi,
                        reason=f"crossref lookup failed: {exc}",
                    )
                )
            else:
                if raw:
                    self._record_provider_success("crossref")
                    candidates.append(
                        self._provider_candidate(
                            "crossref",
                            raw,
                            canonical_doi=canonical_doi,
                            reason="DOI identity enriched via Crossref",
                        )
                    )
        if (
            canonical_s2
            and self.options.allow_s2
            and self._acquire_provider_call("s2")
        ):
            try:
                raw = (
                    self.options.s2_provider
                    or make_default_s2_provider()
                )(canonical_s2)
            except Exception as exc:
                self._record_provider_error("s2")
                candidates.append(
                    self._provider_candidate(
                        "s2_provider",
                        {},
                        canonical_s2=canonical_s2,
                        reason=f"S2 lookup failed: {exc}",
                    )
                )
            else:
                if raw:
                    self._record_provider_success("s2")
                    candidates.append(
                        self._provider_candidate(
                            "s2_provider",
                            raw,
                            canonical_s2=canonical_s2,
                            reason="S2 paper identity enriched via Semantic Scholar",
                        )
                    )

    def _acquire_provider_call(self, provider_name: str) -> bool:
        """Atomically claim one slot of the shared provider-call budget."""

        with self._stats_lock:
            if self.options.max_provider_calls > 0:
                used = sum(self.provider_calls.values())
                if used >= self.options.max_provider_calls:
                    return False
            self.provider_calls[provider_name] = (
                self.provider_calls.get(provider_name, 0) + 1
            )
            return True

    def _record_provider_success(self, provider_name: str) -> None:
        with self._stats_lock:
            self.provider_successes[provider_name] = (
                self.provider_successes.get(provider_name, 0) + 1
            )

    def _record_provider_error(self, provider_name: str) -> None:
        with self._stats_lock:
            self.provider_errors[provider_name] = (
                self.provider_errors.get(provider_name, 0) + 1
            )

    def _provider_candidate(
        self,
        source: str,
        raw: Mapping[str, Any],
        *,
        canonical_doi: str = "",
        canonical_s2: str = "",
        lookup_kind: str = "",
        reason: str,
    ) -> dict[str, Any]:
        year, year_reason = _valid_year(raw.get("year"))
        if year == "" and str(raw.get("year") or "").strip() == PLACEHOLDER_YEAR:
            with self._stats_lock:
                self.placeholder_year_rejections += 1
        authors, author_reason = _valid_authors(raw.get("authors"))
        venue, venue_reason = _valid_venue(raw.get("venue"))
        title, title_reason = _valid_title(raw.get("title"))
        doi = _valid_doi(raw.get("doi") or canonical_doi)
        s2_id = normalize_s2_id(raw.get("s2_id") or canonical_s2)
        url = _valid_url(raw.get("url")) or (
            f"https://doi.org/{doi}" if doi else ""
        )
        self._count_corrupt_rejections(
            (
                ("title", title_reason),
                ("authors", author_reason),
                ("venue", venue_reason),
            )
        )
        return {
            "source": source,
            "base_source": source,
            "source_path": "",
            "section_id": None,
            "trust_type": "provider_verified",
            "retrieval_origin": "",
            "exact": False,
            "confidence": "high",
            "reason": reason,
            "lookup_kind": lookup_kind or "",
            "fields": {
                "title": title,
                "authors": authors,
                "year": year,
                "venue": venue,
                "doi": doi,
                "url": url,
                "s2_id": s2_id,
            },
            "missing_reasons": {
                "title": title_reason,
                "year": year_reason,
                "authors": author_reason,
                "venue": venue_reason,
            },
            "aliases": [f"doi:{doi}"] if doi else [],
        }

    def _publication_info_missing(
        self, candidates: Sequence[Mapping[str, Any]]
    ) -> list[str]:
        merged = self._merge_fields(candidates)
        return [
            field
            for field in ("title", "authors", "year", "venue")
            if _empty(merged.get(field))
        ]

    def _merge_fields(
        self, candidates: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for candidate in sorted(
            candidates,
            key=lambda candidate: self._candidate_rank(candidate),
            reverse=True,
        ):
            for field in BIBLIOGRAPHIC_FIELDS:
                value = candidate.get("fields", {}).get(field)
                if _empty(merged.get(field)) and not _empty(value):
                    merged[field] = value
        return merged

    def _merge_entry(
        self,
        candidates: Sequence[Mapping[str, Any]],
        identity: RefIdentity,
        marker_count: int,
        sections: list[str],
    ) -> dict[str, Any]:
        fields = self._merge_fields(candidates)
        provenance: dict[str, dict[str, Any]] = {}
        missing_reasons: dict[str, list[str]] = {}
        quality_rejections: list[dict[str, str]] = []
        aliases: list[str] = [identity.token]
        for candidate in sorted(
            candidates,
            key=lambda candidate: self._candidate_rank(candidate),
            reverse=True,
        ):
            candidate_fields = candidate.get("fields", {})
            for field in BIBLIOGRAPHIC_FIELDS:
                if field not in provenance and not _empty(
                    candidate_fields.get(field)
                ):
                    provenance[field] = {
                        "source": candidate.get("source") or "",
                        "base_source": candidate.get("base_source") or "",
                        "source_path": candidate.get("source_path") or "",
                        "section_id": candidate.get("section_id"),
                        "confidence": candidate.get("confidence") or "",
                        "exact_identity_match": bool(candidate.get("exact")),
                        "reason": candidate.get("reason") or "",
                    }
            for alias in candidate.get("aliases") or []:
                if alias and alias not in aliases:
                    aliases.append(alias)
            for field, reason in (candidate.get("missing_reasons") or {}).items():
                if reason:
                    missing_reasons.setdefault(field, [])
                    if reason not in missing_reasons[field]:
                        missing_reasons[field].append(reason)
                    if reason.startswith("corrupt metadata rejected"):
                        rejection = {
                            "field": field,
                            "reason": reason,
                            "source": str(candidate.get("source") or ""),
                            "source_path": str(candidate.get("source_path") or ""),
                        }
                        if rejection not in quality_rejections:
                            quality_rejections.append(rejection)

        for field in BIBLIOGRAPHIC_FIELDS:
            if _empty(fields.get(field)):
                reasons = missing_reasons.get(field) or [
                    f"no {field} recovered from any local source or provider"
                ]
                provenance[field] = {
                    "status": "missing",
                    "reasons": sorted(set(reasons)),
                }

        entry_fields = {
            field: (
                fields.get(field, _field_default(field))
                if not _empty(fields.get(field))
                else _field_default(field)
            )
            for field in BIBLIOGRAPHIC_FIELDS
        }
        missing_fields = [
            field for field in BIBLIOGRAPHIC_FIELDS if _empty(entry_fields[field])
        ]
        s2_id = self._merge_s2_id(candidates)
        if not s2_id and identity.kind in ("s2", "corpusid"):
            candidate_s2 = identity.value.casefold()
            if S2_HASH_PATTERN.fullmatch(candidate_s2) or candidate_s2.startswith(
                "corpusid:"
            ):
                s2_id = candidate_s2
        resolution_notes: list[str] = []
        for field in REQUIRED_COMPLETENESS_FIELDS + ("venue",):
            if _empty(entry_fields[field]):
                reasons = provenance.get(field, {}).get("reasons", [])
                resolution_notes.append(
                    f"{field}: " + "; ".join(reasons) if reasons else f"{field}: missing"
                )
        status = self._status(entry_fields)
        if status == "unresolved":
            resolution_notes.append(
                "no title or stable locator (DOI/URL) recovered; entry is "
                "transparently unresolved"
            )
        return {
            "identity": identity.token,
            "identity_kind": identity.kind,
            "canonical_identity": self._canonical_identity(
                entry_fields, s2_id, identity
            ),
            "dedupe_key": self._dedupe_key(entry_fields, s2_id, identity),
            "aliases": aliases,
            "markers": [identity.token],
            "marker_count": marker_count,
            "sections": list(sections),
            "title": entry_fields["title"],
            "authors": entry_fields["authors"],
            "year": entry_fields["year"],
            "venue": entry_fields["venue"],
            "doi": entry_fields["doi"],
            "url": entry_fields["url"],
            "s2_id": s2_id,
            "provenance": provenance,
            "resolution_status": status,
            "missing_fields": missing_fields,
            "quality_rejections": sorted(
                quality_rejections,
                key=lambda item: (
                    item["field"],
                    item["reason"],
                    item["source"],
                ),
            ),
            "resolution_notes": sorted(set(resolution_notes)),
            "candidate_sources": sorted(
                {
                    str(candidate.get("source") or "")
                    for candidate in candidates
                }
            ),
        }

    def _status(self, fields: Mapping[str, Any]) -> str:
        """Classify basic internal completeness as title + authors + year.

        Venue/journal and DOI/URL remain preferred enrichment targets, but
        their absence is recorded in ``missing_fields`` and notes rather than
        flipping an otherwise complete reference to partial.  The submission
        profile keeps its own, stricter stable-locator and author gates.
        """

        has_title = not _empty(fields.get("title"))
        has_locator = not _empty(fields.get("doi")) or not _empty(fields.get("url"))
        complete = all(
            not _empty(fields.get(field))
            for field in REQUIRED_COMPLETENESS_FIELDS
        )
        if complete:
            return "resolved"
        if has_title or has_locator:
            return "partial"
        return "unresolved"

    def _canonical_identity(
        self,
        fields: Mapping[str, Any],
        s2_id: str,
        identity: RefIdentity | str,
    ) -> str:
        identity = self._coerce_identity(identity)
        doi = _valid_doi(fields.get("doi"))
        if doi:
            return f"doi:{doi}"
        if s2_id and S2_HASH_PATTERN.fullmatch(s2_id):
            return f"s2:{s2_id}"
        title = _clean_unicode(fields.get("title") or "").strip()
        if title:
            return f"title:{normalize_title(title)[:160]}"
        return identity.token

    def _dedupe_key(
        self,
        fields: Mapping[str, Any],
        s2_id: str,
        identity: RefIdentity | str,
    ) -> dict[str, str]:
        identity = self._coerce_identity(identity)
        doi = _valid_doi(fields.get("doi"))
        if doi:
            return {"priority": "doi", "value": doi}
        if s2_id and S2_HASH_PATTERN.fullmatch(s2_id):
            return {"priority": "s2", "value": s2_id}
        title = _clean_unicode(fields.get("title") or "").strip()
        if title:
            return {"priority": "title", "value": normalize_title(title)}
        return {"priority": "identity", "value": identity.token}

    def _coerce_identity(self, identity: RefIdentity | str) -> RefIdentity:
        if isinstance(identity, RefIdentity):
            return identity
        parsed = parse_ref_identity(identity)
        if parsed is not None:
            return parsed
        return RefIdentity(
            token=identity,
            kind="other",
            value=identity,
            normalized=f"other:{identity.casefold()}",
        )

    def _merge_s2_id(
        self, candidates: Sequence[Mapping[str, Any]]
    ) -> str:
        for candidate in sorted(
            candidates,
            key=lambda candidate: self._candidate_rank(candidate),
            reverse=True,
        ):
            s2_id = normalize_s2_id(candidate.get("fields", {}).get("s2_id") or "")
            if s2_id:
                return s2_id
        for candidate in candidates:
            for alias in candidate.get("aliases") or []:
                text = _clean_unicode(alias).strip()
                parsed = parse_ref_identity(text)
                if parsed is not None and parsed.kind == "s2":
                    return parsed.value
        return ""

    def _dedupe_entries(
        self, entries: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for entry in entries:
            key = entry["dedupe_key"]
            group_key = (key["priority"], key["value"])
            groups.setdefault(group_key, []).append(entry)
        merged_entries: list[dict[str, Any]] = []
        for group_key in sorted(groups):
            group = groups[group_key]
            merged_entries.append(
                self._merge_entry_group(group) if len(group) > 1 else group[0]
            )
        return sorted(
            merged_entries,
            key=lambda entry: entry["canonical_identity"],
        )

    def _merge_entry_group(
        self, entries: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        first = entries[0]
        aliases: list[str] = []
        markers: list[str] = []
        sections: list[str] = []
        marker_count = 0
        for entry in entries:
            for alias in entry["aliases"]:
                if alias not in aliases:
                    aliases.append(alias)
            for marker in entry["markers"]:
                if marker not in markers:
                    markers.append(marker)
            for section in entry["sections"]:
                if section not in sections:
                    sections.append(section)
            marker_count += int(entry.get("marker_count") or 0)

        fields: dict[str, Any] = {}
        provenance: dict[str, dict[str, Any]] = {}
        conflicts: list[str] = []
        for entry in entries:
            for field in BIBLIOGRAPHIC_FIELDS:
                value = entry.get(field)
                if _empty(value):
                    continue
                if field not in fields:
                    fields[field] = value
                    provenance[field] = entry["provenance"].get(field, {})
                elif fields[field] != value and _empty(provenance.get(field, {}).get("status")):
                    conflicts.append(
                        f"{field}: {fields[field]!r} vs {value!r}"
                    )
        for field in BIBLIOGRAPHIC_FIELDS:
            if field not in fields:
                provenance[field] = {
                    "status": "missing",
                    "reasons": [
                        f"no {field} recovered from any local source or provider"
                    ],
                }

        entry_fields = {
            field: (
                fields.get(field, _field_default(field))
                if not _empty(fields.get(field))
                else _field_default(field)
            )
            for field in BIBLIOGRAPHIC_FIELDS
        }
        s2_id = next(
            (
                normalize_s2_id(entry.get("s2_id") or "")
                for entry in entries
                if normalize_s2_id(entry.get("s2_id") or "")
            ),
            "",
        )
        first_identity = self._coerce_identity(first["identity"])
        missing_fields = [
            field for field in BIBLIOGRAPHIC_FIELDS if _empty(entry_fields[field])
        ]
        status = self._status(entry_fields)
        merged = dict(first)
        merged.update(
            {
                "canonical_identity": self._canonical_identity(
                    entry_fields, s2_id, first_identity
                ),
                "dedupe_key": self._dedupe_key(
                    entry_fields, s2_id, first_identity
                ),
                "aliases": aliases,
                "markers": markers,
                "marker_count": marker_count,
                "sections": sections,
                "title": entry_fields["title"],
                "authors": entry_fields["authors"],
                "year": entry_fields["year"],
                "venue": entry_fields["venue"],
                "doi": entry_fields["doi"],
                "url": entry_fields["url"],
                "s2_id": s2_id,
                "provenance": provenance,
                "resolution_status": status,
                "missing_fields": missing_fields,
                "quality_rejections": sorted(
                    {
                        json.dumps(item, ensure_ascii=False, sort_keys=True): item
                        for entry in entries
                        for item in entry.get("quality_rejections") or []
                    }.values(),
                    key=lambda item: (item["field"], item["reason"], item["source"]),
                ),
                "resolution_notes": sorted(
                    {
                        note
                        for entry in entries
                        for note in entry.get("resolution_notes") or []
                    }
                ),
                "candidate_sources": sorted(
                    {
                        source
                        for entry in entries
                        for source in entry.get("candidate_sources") or []
                    }
                ),
                "dedupe_conflicts": sorted(set(conflicts)),
            }
        )
        return merged

    def _renderer_records(
        self, entries: Sequence[Mapping[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        for entry in entries:
            base = {
                "paper_id": entry["canonical_identity"],
                "title": entry["title"],
                "authors": entry["authors"],
                "year": entry["year"],
                "venue": entry["venue"],
                "doi": entry["doi"],
                "url": entry["url"],
                "s2_paper_id": entry.get("s2_id") or "",
                "reference_kind": "article" if entry.get("venue") else "misc",
                "metadata_source": self._top_source(entry),
                "resolution_status": entry["resolution_status"],
                "missing_fields": entry["missing_fields"],
                "markers": entry["markers"],
                "marker_count": entry["marker_count"],
            }
            records[entry["canonical_identity"]] = dict(base)
            for alias in entry["aliases"]:
                records[alias] = dict(base, paper_id=alias)
        return records

    def _top_source(self, entry: Mapping[str, Any]) -> str:
        title_provenance = entry.get("provenance", {}).get("title", {})
        source = str(title_provenance.get("source") or "")
        if source == "title_fallback":
            return str(title_provenance.get("base_source") or "title_fallback")
        return source or str(title_provenance.get("status") or "unresolved")

    def _build_audit(
        self,
        entries: Sequence[Mapping[str, Any]],
        inventory: Mapping[str, Any],
    ) -> dict[str, Any]:
        identity_kinds: dict[str, int] = {}
        for token in inventory["unique_tokens"]:
            parsed = parse_ref_identity(token)
            kind = parsed.kind if parsed is not None else "malformed"
            identity_kinds[kind] = identity_kinds.get(kind, 0) + 1
        missing_field_counts = {
            field: 0 for field in BIBLIOGRAPHIC_FIELDS
        }
        source_counts: dict[str, int] = {}
        for entry in entries:
            for field in entry["missing_fields"]:
                missing_field_counts[field] += 1
            for source in entry.get("candidate_sources") or []:
                source_counts[source] = source_counts.get(source, 0) + 1
        status_counts = {
            "resolved": sum(
                1 for entry in entries if entry["resolution_status"] == "resolved"
            ),
            "partial": sum(
                1 for entry in entries if entry["resolution_status"] == "partial"
            ),
            "unresolved": sum(
                1 for entry in entries if entry["resolution_status"] == "unresolved"
            ),
        }
        unique_identities = len(inventory["unique_tokens"])
        total_markers = int(inventory["total_occurrences"])
        return {
            "total_ref_markers": total_markers,
            "unique_ref_identities": unique_identities,
            "catalog_entry_count": len(entries),
            "deduplicated_identity_count": max(
                0, unique_identities - len(entries)
            ),
            "resolution_status_counts": status_counts,
            "identity_kind_counts": dict(sorted(identity_kinds.items())),
            "missing_field_counts": missing_field_counts,
            "source_counts": dict(sorted(source_counts.items())),
            "enriched_by_openalex_count": self.provider_successes["openalex"],
            "enriched_by_crossref_count": self.provider_successes["crossref"],
            "enriched_by_s2_count": self.provider_successes["s2"],
            "provider_calls": dict(self.provider_calls),
            "provider_errors": dict(self.provider_errors),
            "placeholder_year_1900_rejected_count": self.placeholder_year_rejections,
            "corrupt_metadata_field_rejections": sum(
                self.corrupt_field_rejections.values()
            ),
            "corrupt_metadata_field_rejections_by_field": dict(
                sorted(self.corrupt_field_rejections.items())
            ),
            "supplemental_metadata_file_count": len(self.index.supplemental_files),
            "supplemental_metadata_record_count": len(
                self.index.supplemental_records
            ),
            "malformed_ref_count": len(inventory["malformed"]),
            "with_doi_count": sum(1 for entry in entries if entry.get("doi")),
            "with_s2_id_count": sum(1 for entry in entries if entry.get("s2_id")),
            "with_title_count": sum(1 for entry in entries if entry.get("title")),
            "with_authors_count": sum(1 for entry in entries if entry.get("authors")),
            "with_year_count": sum(1 for entry in entries if entry.get("year")),
            "with_venue_count": sum(1 for entry in entries if entry.get("venue")),
            "with_url_count": sum(1 for entry in entries if entry.get("url")),
        }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )


def build_publication_metadata_catalog(
    *,
    staged_manuscript_path: str | Path,
    handoff_path: str | Path,
    project_root: str | Path | None,
    output_dir: str | Path,
    options: ResolverOptions | None = None,
    staged_context_path: str | Path | None = None,
    material_cache_dirs: Sequence[str | Path] = (),
    scan_material_caches: bool = True,
    max_material_cache_roots: int = 8,
    supplemental_metadata_paths: Sequence[str | Path] = (),
    s2_cache_path: str | Path | None = None,
    include_s2_cache: bool = True,
    verify_digests: bool = True,
) -> dict[str, Any]:
    """Resolve the staged manuscript and write the catalog + audit files."""

    manuscript_path = Path(staged_manuscript_path)
    handoff_path = Path(handoff_path)
    if not manuscript_path.is_file():
        raise PublicationMetadataError(
            f"staged manuscript not found: {manuscript_path}"
        )
    if not handoff_path.is_file():
        raise PublicationMetadataError(f"unified handoff not found: {handoff_path}")
    project_root = infer_publication_project_root(
        handoff_path,
        explicit_project_root=project_root,
    )
    manuscript_text = _read_text(manuscript_path, "staged manuscript")

    index = LocalMetadataIndex(
        project_root=project_root,
        handoff_path=handoff_path,
        staged_context_path=staged_context_path,
        material_cache_dirs=material_cache_dirs,
        scan_material_caches=scan_material_caches,
        max_material_cache_roots=max_material_cache_roots,
        supplemental_metadata_paths=supplemental_metadata_paths,
        s2_cache_path=s2_cache_path,
        include_s2_cache=include_s2_cache,
        verify_digests=verify_digests,
    )
    resolver = PublicationMetadataResolver(
        project_root=project_root,
        index=index,
        options=options,
    )
    catalog = resolver.resolve(manuscript_text)
    input_files = index.source_file_records()
    manuscript_relative = _project_relative(manuscript_path, project_root)
    handoff_relative = _project_relative(handoff_path, project_root)
    input_files.append({"path": manuscript_relative, "sha256": _sha256_file(manuscript_path)})
    input_files = sorted(
        {item["path"]: item for item in input_files}.values(),
        key=lambda item: item["path"],
    )
    catalog["input"] = {
        "staged_manuscript": manuscript_relative,
        "unified_handoff": handoff_relative,
        "project_root": ".",
        "input_files": input_files,
    }
    catalog["input_fingerprint"] = fingerprint(
        {
            "schema_version": SCHEMA_VERSION,
            "input_files": input_files,
            "options": {
                "allow_openalex": (
                    bool(options.allow_openalex) if options else False
                ),
                "allow_crossref": bool(options.allow_crossref) if options else False,
                "allow_s2": bool(options.allow_s2) if options else False,
                "max_provider_calls": options.max_provider_calls if options else 0,
            },
        }
    )
    content = {
        key: value
        for key, value in catalog.items()
        if key not in ("catalog_fingerprint",)
    }
    catalog["catalog_fingerprint"] = fingerprint(content)

    output_dir = Path(output_dir)
    catalog_path = output_dir / CATALOG_FILENAME
    audit_path = output_dir / AUDIT_FILENAME
    _write_json(
        catalog_path,
        catalog,
    )
    _write_json(
        audit_path,
        {
            "schema_version": SCHEMA_VERSION,
            "input_fingerprint": catalog["input_fingerprint"],
            "catalog_fingerprint": catalog["catalog_fingerprint"],
            "audit": catalog["audit"],
        },
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "staged_manuscript": manuscript_relative,
        "unified_handoff": handoff_relative,
        "input_fingerprint": catalog["input_fingerprint"],
        "catalog_fingerprint": catalog["catalog_fingerprint"],
        "output_paths": {
            "catalog": str(catalog_path),
            "audit": str(audit_path),
        },
        "audit": catalog["audit"],
        "malformed_refs": catalog["malformed_refs"],
    }


__all__ = [
    "SCHEMA_VERSION",
    "CATALOG_FILENAME",
    "AUDIT_FILENAME",
    "BIBLIOGRAPHIC_FIELDS",
    "PublicationMetadataError",
    "RefIdentity",
    "parse_ref_identity",
    "normalize_doi",
    "normalize_s2_id",
    "normalize_title",
    "doi_from_chunk_id",
    "identity_lookup_keys",
    "marker_occurrences",
    "inventory_ref_identities",
    "LocalMetadataIndex",
    "ResolverOptions",
    "PublicationMetadataResolver",
    "OpenAlexProvider",
    "make_default_crossref_provider",
    "make_default_openalex_provider",
    "make_default_s2_provider",
    "canonical_json",
    "fingerprint",
    "build_publication_metadata_catalog",
]
