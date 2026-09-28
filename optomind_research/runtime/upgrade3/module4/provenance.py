"""Program-owned source anchors and reverse indexes for Module 4."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value)).strip()


def block_map(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        _text(row.get("block_id")): dict(row)
        for row in snapshot.get("blocks") or ()
        if isinstance(row, Mapping) and _text(row.get("block_id"))
    }


def resolve_anchor(
    snapshot: Mapping[str, Any],
    *,
    block_id: str = "",
    quote: str = "",
    snapshot_id: str = "",
    anchor_type: str = "text",
    source_binding: str = "exact_quote",
) -> dict[str, Any]:
    """Resolve a model candidate against frozen local source text.

    A model may suggest a block and quote, but only this function can mark an
    anchor bound.  Unknown blocks, cross-snapshot suggestions, and ambiguous
    duplicate quotes remain visible as unresolved records.
    """

    current_snapshot = _text(snapshot.get("snapshot_id"))
    anchor: dict[str, Any] = {
        "snapshot_id": current_snapshot,
        "source_document_id": "",
        "block_id": _text(block_id),
        "locator": {},
        "anchor_type": anchor_type or "text",
        "quote_original": _text(quote),
        "text_hash": "",
        "binding_status": "unresolved",
        "binding_reason": "",
    }
    whole_block = _text(source_binding).casefold() == "whole_block"
    if whole_block:
        anchor["source_binding"] = "program_whole_block"
    if snapshot_id and snapshot_id != current_snapshot:
        anchor["binding_reason"] = "cross_snapshot_anchor"
        return anchor
    row = block_map(snapshot).get(_text(block_id))
    if row is None:
        anchor["binding_reason"] = "unknown_block"
        return anchor
    anchor["source_document_id"] = _text(row.get("source_document_id"))
    anchor["locator"] = dict(row.get("locator") or {})
    # ``text_normalized`` is already the frozen coordinate space.  Do not
    # normalize it again here: changing whitespace would invalidate offsets.
    source = _text(row.get("text_normalized") or row.get("text_raw"))
    # In whole-block mode the program owns the quote.  The model's quote is
    # intentionally ignored, so a paraphrase or fabricated substring cannot
    # become evidence merely because it names a real block.
    candidate = source if whole_block else _text(quote)
    if not candidate:
        anchor["binding_reason"] = "quote_missing"
        return anchor
    occurrences = [match.start() for match in re.finditer(re.escape(candidate), source)] if candidate else []
    if not occurrences:
        anchor["binding_reason"] = "quote_not_found"
        return anchor
    if len(occurrences) > 1:
        anchor["binding_reason"] = "ambiguous_quote"
        anchor["quote_original"] = candidate
        return anchor
    start = occurrences[0]
    end = start + len(candidate)
    anchor.update({
        "quote_original": source[start:end],
        "char_start": start,
        "char_end": end,
        "text_hash": _text(row.get("text_hash")) or hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "binding_status": "bound",
        "binding_reason": "exact_normalized_quote",
    })
    return anchor


def anchors_for_candidates(
    snapshot: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
    *,
    default_block_ids: Iterable[str] = (),
    default_snapshot_id: str = "",
    source_binding: str = "exact_quote",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve candidate anchor rows and return ``(anchors, issues)``."""

    anchors: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    defaults = [_text(value) for value in default_block_ids if _text(value)]
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        source_pairs = candidate.get("sources")
        pairs: list[tuple[str, str]] = []
        if isinstance(source_pairs, Mapping):
            source_pairs = [source_pairs]
        if isinstance(source_pairs, (list, tuple)) and source_pairs:
            for source in source_pairs:
                if not isinstance(source, Mapping):
                    issues.append({"code": "source_pair_invalid", "draft": dict(candidate)})
                    continue
                pairs.append((_text(source.get("block_id") or source.get("source_block_id")), _text(source.get("quote") or source.get("quote_original") or source.get("source_quote"))))
        else:
            block_ids = candidate.get("source_block_ids") or candidate.get("block_ids") or candidate.get("source_ids") or ()
            if isinstance(block_ids, str):
                block_ids = [block_ids]
            block_ids = [_text(value) for value in block_ids if _text(value)] or defaults
            quote = _text(candidate.get("quote") or candidate.get("source_quote") or candidate.get("quote_original"))
            pairs = [(block_id, quote) for block_id in block_ids]
        if not pairs or any(not block_id for block_id, _ in pairs):
            issues.append({"code": "draft_without_source_block", "draft": dict(candidate)})
            continue
        for block_id, quote in pairs:
            anchor = resolve_anchor(
                snapshot,
                block_id=block_id,
                quote=quote,
                snapshot_id=_text(candidate.get("snapshot_id") or default_snapshot_id),
                anchor_type=_text(candidate.get("anchor_type") or "text"),
                source_binding=source_binding,
            )
            anchor["anchor_id"] = "anchor-" + _hash({key: anchor.get(key) for key in ("snapshot_id", "block_id", "char_start", "char_end", "quote_original")})[:20]
            if anchor["anchor_id"] in seen:
                continue
            seen.add(anchor["anchor_id"])
            anchors.append(anchor)
            if anchor["binding_status"] != "bound":
                issues.append({"code": "source_anchor_unresolved", "anchor_id": anchor["anchor_id"], "reason": anchor.get("binding_reason"), "block_id": block_id})
    return anchors, issues


def build_reverse_index(dossier: Mapping[str, Any]) -> dict[str, Any]:
    reverse: dict[str, dict[str, list[str]]] = {}
    for key in ("content_units", "facet_analyses", "model_interpretations", "unmapped_observations"):
        for item in dossier.get(key) or ():
            if not isinstance(item, Mapping):
                continue
            item_id = _text(item.get("unit_id") or item.get("facet_id") or item.get("interpretation_id") or item.get("observation_id"))
            for anchor_id in item.get("source_anchor_ids") or ():
                aid = _text(anchor_id)
                if aid:
                    reverse.setdefault(aid, {}).setdefault(key, []).append(item_id)
            for block_id in item.get("source_block_ids") or ():
                bid = _text(block_id)
                if bid:
                    reverse.setdefault("block:" + bid, {}).setdefault(key, []).append(item_id)
    return reverse


__all__ = ["block_map", "resolve_anchor", "anchors_for_candidates", "build_reverse_index"]
