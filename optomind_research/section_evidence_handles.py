"""Deterministic paragraph-local evidence handles for section writing.

Compact mode replaces long canonical paper/chunk identifiers in the paragraph
payload with stable local handles (``E01``, ``E02``, ...). The registry keeps
the exact mapping from handle to canonical marker, paper_id, chunk_id, exact
spans, and other evidence metadata so local code can restore canonical
``[REF:paper_id]`` markers before citation validation and persistence.

Resolution is exact-only: a handle is valid iff it is present in the registry
built from that paragraph's evidence list.  There is no fuzzy, approximate, or
nearest-marker recovery.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

HANDLE_PREFIX = "E"
_REF_PATTERN = re.compile(r"\[REF:([^\]]+)\]")
_HANDLE_MARKER_PATTERN = re.compile(
    r"\[((?:REF:)?[Ee]\d{2,}(?:\s*[,;]\s*(?:REF:)?[Ee]\d{2,})*)\]"
)
_MALFORMED_HANDLE_PATTERN = re.compile(
    r"\[(?:REF:)?[Ee]\d{2,}:[^\]]*\]"
)
_RESIDUAL_HANDLE_BRACKET_PATTERN = re.compile(
    r"\[[^\]]*\b[Ee]\d{2,}\b[^\]]*\]"
)


def _split_handle_items(body: str) -> list[str]:
    """Split a single or compound handle marker into exact item bodies."""
    items: list[str] = []
    for raw in re.split(r"\s*[,;]\s*", str(body or "").strip()):
        item = raw.strip()
        if item.startswith("REF:"):
            item = item[4:].strip()
        if item:
            items.append(item)
    return items


class EvidenceLike(Protocol):
    """Minimal evidence shape accepted by the handle registry."""

    paper_id: str
    chunk_id: str
    claim_id: str
    exact_spans: list[str]
    source_title: str
    limitations: list[str]
    evidence_level: str
    support_relation: str
    source_kind: str
    scope_fit: str
    retrieval_role: str


def _compact_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _first_sentence_excerpt(value: Any, limit: int = 240) -> str:
    """Short normalized first sentence of an exact span."""
    text = _compact_text(value)
    if not text:
        return ""
    match = re.search(r"[.!?;](?:\s|$)", text)
    if match:
        text = text[: match.end()].strip()
    if len(text) > limit:
        cut = text[:limit].rfind(" ")
        text = text[:cut].strip() if cut > 0 else text[:limit].strip()
    return text


def _semantic_label(evidence: EvidenceLike) -> str:
    """Local, deterministic label from title and the first exact-span sentence."""
    title = _compact_text(getattr(evidence, "source_title", ""))[:160]
    spans = [
        _compact_text(span)
        for span in (getattr(evidence, "exact_spans", None) or [])
        if _compact_text(span)
    ]
    span_excerpt = _first_sentence_excerpt(spans[0], 240) if spans else ""
    parts = [part for part in (title, span_excerpt) if part]
    if parts:
        return " | ".join(parts)
    kind = _compact_text(getattr(evidence, "source_kind", "")) or "evidence"
    return f"{kind} source"


@dataclass
class EvidenceHandleRegistry:
    """Stable E01/E02... mapping for one paragraph's exact evidence list."""

    evidence_packets: list[EvidenceLike] = field(default_factory=list)
    _entries: dict[str, dict[str, Any]] = field(
        init=False, repr=False, default_factory=dict
    )
    _handles_by_paper: dict[str, list[str]] = field(
        init=False, repr=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        self._entries = {}
        self._handles_by_paper = {}
        for index, evidence in enumerate(self.evidence_packets, 1):
            handle = f"{HANDLE_PREFIX}{index:02d}"
            paper_id = _compact_text(getattr(evidence, "paper_id", ""))
            self._entries[handle] = {
                "handle": handle,
                "canonical_marker": paper_id,
                "paper_id": paper_id,
                "chunk_id": _compact_text(getattr(evidence, "chunk_id", "")),
                "claim_id": _compact_text(getattr(evidence, "claim_id", "")),
                "exact_spans": [
                    _compact_text(span)
                    for span in (getattr(evidence, "exact_spans", None) or [])
                ],
                "semantic_label": _semantic_label(evidence),
                "limitations": [
                    _compact_text(limit)
                    for limit in (getattr(evidence, "limitations", None) or [])
                ],
                "evidence_level": _compact_text(
                    getattr(evidence, "evidence_level", "")
                ),
                "support_relation": _compact_text(
                    getattr(evidence, "support_relation", "")
                ),
                "source_kind": _compact_text(getattr(evidence, "source_kind", "")),
                "scope_fit": _compact_text(getattr(evidence, "scope_fit", "")),
                "retrieval_role": _compact_text(
                    getattr(evidence, "retrieval_role", "")
                ),
            }
            self._handles_by_paper.setdefault(paper_id, []).append(handle)

    @property
    def handles(self) -> tuple[str, ...]:
        """Handles in stable E01, E02, ... assignment order."""
        return tuple(self._entries)

    def allowed_markers(self) -> set[str]:
        """The only REF markers a compact-mode model may emit."""
        return set(self._entries)

    def handles_for_paper(self, paper_id: str) -> list[str]:
        """Stable handles mapped to one canonical paper id."""
        return list(self._handles_by_paper.get(paper_id, []))

    def entry(self, handle: str) -> dict[str, Any] | None:
        """Full local metadata for one handle, or None when unknown."""
        return self._entries.get(handle)

    def compact_rows(self) -> list[dict[str, Any]]:
        """Serializable payload rows without long canonical identifiers."""
        rows = []
        for handle in self.handles:
            entry = self._entries[handle]
            rows.append({
                "handle": handle,
                "semantic_label": entry["semantic_label"],
                "exact_text": " ".join(entry["exact_spans"]),
                "limitations": entry["limitations"],
                "evidence_level": entry["evidence_level"],
                "claim_id": entry["claim_id"],
                "support_relation": entry["support_relation"],
                "source_kind": entry["source_kind"],
                "scope_fit": entry["scope_fit"],
            })
        return rows

    def to_dict(self) -> dict[str, Any]:
        """Full serializable registry for local diagnostics and audits."""
        return {
            "mode": "compact_evidence_handles",
            "handles": list(self.handles),
            "entries": dict(self._entries),
        }

    def markers(self, text: str) -> set[str]:
        """All REF marker bodies present in text."""
        return {
            body.strip()
            for body in _REF_PATTERN.findall(str(text or ""))
            if body.strip()
        }

    def handle_markers(self, text: str) -> set[str]:
        """Bare or REF handle bodies from single and compound markers.

        Handles are matched case-insensitively so malformed lowercase forms
        are detected and fail closed; only exact registered uppercase handles
        can be resolved.
        """
        markers: set[str] = set()
        for match in _HANDLE_MARKER_PATTERN.finditer(str(text or "")):
            markers.update(_split_handle_items(match.group(1)))
        return markers

    def non_handle_reference_markers(self, text: str) -> set[str]:
        """REF marker bodies outside recognized handle compounds."""
        masked = _HANDLE_MARKER_PATTERN.sub(" ", str(text or ""))
        return {
            body.strip()
            for body in _REF_PATTERN.findall(masked)
            if body.strip()
        }

    def malformed_handle_markers(self, text: str) -> list[str]:
        """Malformed mixed handle tokens such as ``[E01:0010]``."""
        return sorted({
            match
            for match in _MALFORMED_HANDLE_PATTERN.findall(str(text or ""))
        })

    def residual_handle_brackets(self, text: str) -> list[str]:
        """Bracketed tokens/lists still containing a handle-shaped item.

        This is the general postcondition after exact resolution: any bracket
        that still contains ``E##`` / ``e##`` is an unresolved compact-handle
        hard failure (mixed non-handle items, trailing delimiters, unsupported
        separators, unknown or lowercase handles, etc.). No fuzzy repair is
        attempted.
        """
        return sorted({
            match
            for match in _RESIDUAL_HANDLE_BRACKET_PATTERN.findall(
                str(text or "")
            )
        })

    def resolve_text(
        self,
        text: str,
    ) -> tuple[str, list[str], list[str]]:
        """Resolve known handles to canonical markers; leave unknowns unchanged.

        Resolves registered ``[E01]`` / ``[REF:E01]`` and compound lists such
        as ``[E21, E46, E47]`` to separate ``[REF:paper_id]`` markers.
        Identical resulting canonical markers are deduplicated in first-seen
        order. Returns ``(resolved_text, resolved_handles, unknown_markers)``.
        If any item is unknown, lowercase, or malformed, the whole compound is
        left unchanged (fail-closed, no partial rewrite).
        """
        resolved_handles: list[str] = []
        unknown_markers: list[str] = []

        def replace(match: re.Match[str]) -> str:
            items = _split_handle_items(match.group(1))
            entries = [self._entries.get(item) for item in items]
            if any(
                entry is None or not entry["paper_id"]
                for entry in entries
            ):
                unknown_markers.extend(
                    item
                    for item, entry in zip(items, entries)
                    if entry is None or not entry["paper_id"]
                )
                return match.group(0)
            seen_papers: set[str] = set()
            parts: list[str] = []
            for item in items:
                entry = self._entries[item]
                paper_id = entry["paper_id"]
                resolved_handles.append(item)
                if paper_id in seen_papers:
                    continue
                seen_papers.add(paper_id)
                parts.append(f"[REF:{paper_id}]")
            return " ".join(parts)

        resolved = _HANDLE_MARKER_PATTERN.sub(replace, str(text or ""))
        return (
            resolved,
            list(dict.fromkeys(resolved_handles)),
            sorted(set(unknown_markers)),
        )

    def encode_markers_to_handles(self, text: str) -> str:
        """Deterministically re-encode canonical paper markers as handles.

        Used to feed a previous attempt back to the model in compact mode.
        When several handles share one paper, the lowest handle is chosen;
        anything not mapped by this registry is left unchanged.
        """
        def replace(match: re.Match[str]) -> str:
            body = match.group(1).strip()
            if not body:
                return match.group(0)
            handles = self._handles_by_paper.get(body, [])
            if handles:
                return f"[REF:{handles[0]}]"
            return match.group(0)

        return _REF_PATTERN.sub(replace, str(text or ""))
