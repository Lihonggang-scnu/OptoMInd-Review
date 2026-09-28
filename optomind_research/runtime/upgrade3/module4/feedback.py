"""Append-only feedback from Module 4 to frozen M3 cards."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import FEEDBACK_SCHEMA_VERSION


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _cards(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        if "cards" in value and value.get("cards") is not None:
            value = value.get("cards")
        elif "items" in value and value.get("items") is not None:
            value = value.get("items")
        else:
            # Real M3 exports are commonly keyed directly by canonical ID.
            value = value
        if isinstance(value, Mapping):
            if any(key in value for key in ("paper_identity", "facet_relevance", "canonical_paper_id")):
                value = [value]
            else:
                rows = []
                for key, item in value.items():
                    if isinstance(item, Mapping):
                        row = dict(item)
                        row.setdefault("canonical_paper_id", key)
                        identity = row.get("paper_identity")
                        if isinstance(identity, Mapping):
                            row["canonical_paper_id"] = identity.get("paper_id") or identity.get("canonical_paper_id") or row["canonical_paper_id"]
                        rows.append(row)
                return rows
    if isinstance(value, Mapping):
        rows = []
        for key, item in value.items():
            if isinstance(item, Mapping):
                row = dict(item)
                row.setdefault("canonical_paper_id", key)
                identity = row.get("paper_identity")
                if isinstance(identity, Mapping):
                    row.setdefault("canonical_paper_id", identity.get("paper_id") or identity.get("canonical_paper_id") or key)
                rows.append(row)
        return rows
    return [dict(item) for item in value or () if isinstance(item, Mapping)]


def _upstream_hash(upstream_snapshot: Any) -> str:
    if isinstance(upstream_snapshot, Mapping):
        return _text(upstream_snapshot.get("snapshot_hash") or upstream_snapshot.get("sha256") or upstream_snapshot.get("hash")) or _hash(upstream_snapshot)
    return _hash(upstream_snapshot)


def build_feedback(dossier: Mapping[str, Any], upstream_snapshot: Any) -> list[dict[str, Any]]:
    """Create one event per original issue/facet without mutating M3."""

    source_hash = _upstream_hash(upstream_snapshot)
    original_cards = _cards(upstream_snapshot)
    paper_id = _text((dossier.get("paper_identity_ref") or dossier.get("paper_identity") or {}).get("canonical_paper_id"))
    facets = { _text(item.get("facet_id")): item for item in dossier.get("facet_analyses") or () if isinstance(item, Mapping) }
    events: list[dict[str, Any]] = []
    bound_anchor_ids = {_text(item.get("anchor_id")) for item in dossier.get("source_anchors") or () if isinstance(item, Mapping) and _text(item.get("binding_status")) == "bound"}
    usable_unit_ids = {
        _text(item.get("unit_id"))
        for item in dossier.get("content_units") or ()
        if isinstance(item, Mapping)
        and _text((item.get("verification_ref") or {}).get("status")) in {"supported_as_report", "supported_as_inference", "partly_supported"}
    }
    semantic_ok = _text(((dossier.get("verification_summary") or {}).get("semantic_review") or {}).get("status")) == "completed"
    for card in original_cards:
        if _text(card.get("canonical_paper_id")) and _text(card.get("canonical_paper_id")) != paper_id:
            continue
        relevance = card.get("facet_relevance") if isinstance(card.get("facet_relevance"), Mapping) else None
        card_facets = [_text(card.get("facet_id") or card.get("id"))] if card.get("facet_id") or card.get("id") else list(relevance or facets)
        for facet_id in card_facets:
            facet_id = _text(facet_id)
            facet = facets.get(facet_id)
            if facet is None:
                continue
            card_facet = relevance.get(facet_id) if isinstance(relevance, Mapping) and isinstance(relevance.get(facet_id), Mapping) else card
            issue_id = _text(card_facet.get("issue_id") or card_facet.get("original_issue_id") or card.get("issue_id") or card.get("original_issue_id"))
            issue_text = _text(card_facet.get("issue_text") or card_facet.get("pending_issue") or card_facet.get("question") or card.get("issue_text") or card.get("pending_issue") or "")
            # A facet alone is not an original M3 issue.  Feedback is only
            # attributable when both the stable issue identity and its text
            # are carried forward from the frozen card.
            if not issue_id or not issue_text:
                continue
            status = _text(facet.get("answer_status") or "uncertain")
            resolution = {"addressed": "resolved", "partly_addressed": "partly_resolved", "not_addressed_in_read_material": "unresolved", "uncertain": "unresolved"}.get(status, "unresolved")
            anchors: list[str] = []
            basis_units: list[str] = []
            for block in facet.get("answer_blocks") or ():
                if isinstance(block, Mapping):
                    anchors.extend(_text(value) for value in block.get("source_anchor_ids") or ())
                    basis_units.extend(_text(value) for value in block.get("content_unit_ids") or block.get("unit_ids") or ())
            event_anchors = list(dict.fromkeys(anchors))
            event_units = list(dict.fromkeys(basis_units))
            answer_reviews_ok = True
            for answer in facet.get("answer_blocks") or ():
                if isinstance(answer, Mapping) and _text(facet.get("answer_status")) in {"addressed", "partly_addressed"}:
                    answer_reviews_ok = _text((answer.get("verification_ref") or {}).get("status")) in {"supported_as_report", "supported_as_inference", "partly_supported"}
                    if not answer_reviews_ok:
                        break
            explicit_negative_reason = bool(_text(facet.get("not_addressed_reason")) or facet.get("limitations_and_counterpoints") or facet.get("coverage_basis"))
            if _text(facet.get("answer_status")) == "not_addressed_in_read_material" and not explicit_negative_reason:
                answer_reviews_ok = False
            evidence_usable = semantic_ok and answer_reviews_ok and bool(event_anchors) and set(event_anchors) <= bound_anchor_ids and (not event_units or set(event_units) <= usable_unit_ids)
            event = {
                "schema_version": FEEDBACK_SCHEMA_VERSION,
                "feedback_id": "feedback-" + _hash({"dossier": dossier.get("dossier_id"), "issue": issue_id, "facet": facet_id})[:24],
                "producer_dossier_id": _text(dossier.get("dossier_id")), "canonical_paper_id": paper_id, "facet_id": facet_id,
                "upstream_snapshot_hash": source_hash,
                "target_path": _text(card_facet.get("target_path") or card.get("target_path") or card.get("path") or ""), "original_issue_id": issue_id, "original_issue_text": issue_text,
                "previous_value": card_facet.get("value", card_facet.get("status", card_facet.get("reading_label"))), "resolution_status": resolution,
                "resolution_text": _text(facet.get("answer_summary") or facet.get("answer") or " ".join(_text(block.get("text") or block.get("statement")) for block in facet.get("answer_blocks") or () if isinstance(block, Mapping))),
                "basis_unit_ids": event_units, "source_anchor_ids": event_anchors,
                "proposed_reading_label": None, "remaining_questions": list(facet.get("limitations_and_counterpoints") or ()),
                "validation_state": "usable" if evidence_usable else "pending_verification", "supersedes_feedback_id": None,
            }
            events.append(event)
    return events


def append_feedback_events(existing: Sequence[Mapping[str, Any]] | str | Path, new_events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(existing, (str, Path)):
        path = Path(existing)
        rows = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                    if isinstance(value, Mapping):
                        rows.append(dict(value))
                except ValueError as exc:
                    raise ValueError(f"invalid_feedback_json:{path}") from exc
    else:
        rows = [dict(item) for item in existing or () if isinstance(item, Mapping)]
    seen = {_text(item.get("feedback_id")) for item in rows}
    for event in new_events:
        row = dict(event)
        fid = _text(row.get("feedback_id"))
        if fid and fid not in seen:
            rows.append(row)
            seen.add(fid)
    return rows


def write_feedback_events(path: str | Path, existing: Sequence[Mapping[str, Any]] | str | Path, new_events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_name(target.name + ".lock")
    deadline = time.time() + 10
    handle = None
    while handle is None:
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.time() > deadline:
                raise TimeoutError(f"feedback_lock_timeout:{target}")
            time.sleep(0.02)
    try:
        rows = append_feedback_events(existing, new_events)
        temp = target.with_name(target.name + ".tmp")
        temp.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + ("\n" if rows else ""), encoding="utf-8")
        os.replace(temp, target)
    finally:
        os.close(handle)
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
    return rows


def build_post_reading_view(original_cards: Any, selected_feedback: Sequence[Mapping[str, Any]], active_dossiers: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    cards = _cards(original_cards)
    dossiers = [dict(item) for item in active_dossiers or () if isinstance(item, Mapping)]
    active_ids = {_text((item.get("paper_identity_ref") or item.get("paper_identity") or {}).get("canonical_paper_id")) for item in dossiers}
    feedback = [dict(item) for item in selected_feedback or () if isinstance(item, Mapping)]
    stale: list[dict[str, Any]] = []
    usable: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    dossier_by_id = {_text(item.get("dossier_id")): item for item in dossiers}
    def card_facet(card: Mapping[str, Any], facet_id: str) -> Mapping[str, Any]:
        relevance = card.get("facet_relevance")
        if isinstance(relevance, Mapping) and isinstance(relevance.get(facet_id), Mapping):
            return relevance[facet_id]
        return card
    for event in feedback:
        producer = _text(event.get("producer_dossier_id"))
        producer_dossier = dossier_by_id.get(producer)
        if producer_dossier is None:
            stale.append({**event, "stale_reason": "producer_dossier_not_active"})
            continue
        expected_hashes = {
            _text(ref.get("sha256"))
            for ref in (producer_dossier.get("input") or {}).get("upstream_snapshot_refs") or ()
            if isinstance(ref, Mapping) and _text(ref.get("sha256"))
        }
        if expected_hashes and _text(event.get("upstream_snapshot_hash")) not in expected_hashes:
            stale.append({**event, "stale_reason": "upstream_snapshot_mismatch"})
            continue
        card_match = next((card for card in cards if _text(card.get("canonical_paper_id")) == _text(event.get("canonical_paper_id")) and (_text(card.get("facet_id") or card.get("id")) == _text(event.get("facet_id")) or isinstance(card.get("facet_relevance"), Mapping) and _text(event.get("facet_id")) in card.get("facet_relevance", {}))), None)
        if card_match is None:
            stale.append({**event, "stale_reason": "target_card_missing"})
            continue
        matched_facet = card_facet(card_match, _text(event.get("facet_id")))
        if _text(event.get("target_path")) and _text(event.get("target_path")) != _text(matched_facet.get("target_path") or matched_facet.get("path") or card_match.get("target_path") or card_match.get("path")):
            stale.append({**event, "stale_reason": "target_path_mismatch"})
            continue
        if _text(event.get("canonical_paper_id")) not in active_ids:
            stale.append({**event, "stale_reason": "active_dossier_identity_missing"})
            continue
        if event.get("validation_state") != "usable":
            stale.append({**event, "stale_reason": "feedback_not_usable"})
            continue
        previous = matched_facet.get("value", matched_facet.get("status", matched_facet.get("reading_label")))
        if event.get("previous_value") != previous:
            stale.append({**event, "stale_reason": "previous_value_mismatch"})
            continue
        active_anchor_ids = {_text(item.get("anchor_id")) for item in producer_dossier.get("source_anchors") or () if isinstance(item, Mapping) and _text(item.get("binding_status")) == "bound"}
        active_unit_ids = {_text(item.get("unit_id")) for item in producer_dossier.get("content_units") or () if isinstance(item, Mapping) and _text((item.get("verification_ref") or {}).get("status")) in {"supported_as_report", "supported_as_inference", "partly_supported"}}
        if not event.get("source_anchor_ids") or not set(_text(item) for item in event.get("source_anchor_ids") or ()) <= active_anchor_ids or not set(_text(item) for item in event.get("basis_unit_ids") or ()) <= active_unit_ids:
            stale.append({**event, "stale_reason": "basis_not_usable"})
            continue
        usable.append(event)
    assessment: list[dict[str, Any]] = []
    for card in cards:
        paper = _text(card.get("canonical_paper_id"))
        facet = _text(card.get("facet_id") or card.get("id"))
        relevance = card.get("facet_relevance") if isinstance(card.get("facet_relevance"), Mapping) else {}
        facet_ids = [facet] if facet else [_text(key) for key in relevance]
        for facet_id in facet_ids:
            matches = [event for event in usable if _text(event.get("canonical_paper_id")) == paper and _text(event.get("facet_id")) == facet_id]
            matched_facet = card_facet(card, facet_id)
            if len({(_text(event.get("upstream_snapshot_hash")), _text(event.get("resolution_text")), _text(event.get("previous_value"))) for event in matches}) > 1:
                conflicts.append({"card": dict(card), "facet_id": facet_id, "events": matches})
            selected = matches[0] if len(matches) == 1 else None
            assessment.append({
                "canonical_paper_id": paper, "facet_id": facet_id,
                "original_issue_id": _text(matched_facet.get("issue_id") or matched_facet.get("original_issue_id") or card.get("issue_id") or card.get("original_issue_id")),
                "resolution_status": _text(selected.get("resolution_status")) if selected else ("conflict" if len(matches) > 1 else "unresolved"),
                "resolution_text": _text(selected.get("resolution_text")) if selected else "",
                "source_anchor_ids": list(selected.get("source_anchor_ids") or ()) if selected else [],
                "feedback_id": _text(selected.get("feedback_id")) if selected else None,
            })
    return {
        "schema_version": FEEDBACK_SCHEMA_VERSION,
        "pre_reading_state": [dict(card) for card in cards],
        "post_reading_assessment": assessment,
        "new_questions": [question for event in usable for question in event.get("remaining_questions") or ()],
        "stale_feedback": stale, "conflicts": conflicts,
        "active_dossier_ids": [_text(item.get("dossier_id")) for item in dossiers],
    }


__all__ = ["build_feedback", "append_feedback_events", "write_feedback_events", "build_post_reading_view"]
