"""Upgrade-3 evidence cards with per-field provenance (ticket 014).

A card is an INDEX into the source, never a replacement for it:

- the model must first select verbatim short quotes; every extracted field
  references those span ids.  Local verification re-checks each quote against
  the canonical text (unicode-folded), verifies that every number claimed in a
  field appears inside its own quoted span, and downgrades any unverifiable
  field to ``not_reported`` with an ``unknown_reason``.
- multi-experiment papers get one card per ``experiment_id``: precision from a
  simulation and speed from a different device can never merge into one row.
  ``experiment_level`` separates material/device results from full-system
  results; ``sim_or_experiment`` must be quoted.
- cards carry the document version (raw hash): a source hash change marks the
  card stale (invalidation), and ``status`` moves
  extracted -> source_checked -> usable_index | needs_source.
- the card itself is never a citation source; relevance is not permission.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any, Dict, List, Optional

CARD_SCHEMA = "optomind.upgrade3.evidence_card.v1"
_PUNCT_MAP = str.maketrans({chr(0x2010): '-', chr(0x2011): '-', chr(0x2012): '-',
                            chr(0x2013): '-', chr(0x2014): '-', chr(0x2212): '-',
                            chr(0x2018): "'", chr(0x2019): "'", chr(0x201C): '"',
                            chr(0x201D): '"', chr(0x00A0): ' '})

_FIELDS = ("method", "results", "conditions", "limitations")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _norm(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text or "").translate(_PUNCT_MAP)
    import re as _re
    return _re.sub(r"\s+", " ", folded.lower()).strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_numbers(text: str) -> List[str]:
    import re as _re
    return _re.findall(r"\d+(?:\.\d+)?", text or "")


def verify_field(quote: str, text: str, value: str = "") -> Optional[str]:
    """Returns None when the quote is verbatim (unicode-folded) and any numbers
    in the value appear in the quote; otherwise the unknown_reason."""
    if not quote or not _norm(quote):
        return "empty_quote"
    if _norm(quote) not in _norm(text):
        return "quote_not_verbatim_in_source"
    if value:
        for num in extract_numbers(value):
            if num not in _norm(quote):
                return f"number_{num}_not_in_quoted_span"
    return None


def build_judge_prompt(contract: Dict[str, Any], paper: Dict[str, Any],
                       max_chars: int = 5200) -> Dict[str, str]:
    text = (paper.get("text") or "").strip()
    system = (
        "You extract evidence cards from ONE paper. Rules: (1) first choose 1-2 "
        "experiments or, for a review/tutorial paper, one survey entry; give each "
        "an experiment_id. (2) Never merge results from different experiments or "
        "devices into one row. (3) Mark sim_or_experiment and experiment_level "
        "(material_device | full_system | not_reported). (4) For every field give "
        "an anchor_quote: a SHORT exact phrase (3-12 words) COPIED "
        "character-for-character from paper_text - the system will locate and "
        "expand it. (5) If the paper does not state something, use not_reported "
        "instead of inventing content. (6) Include limitations honestly. JSON only.")
    user = {
        "review_object": contract.get("object_phrases") or [],
        "paper_title": paper.get("title"),
        "paper_text": text[:max_chars],
        "output_schema": {
            "experiments": [{
                "experiment_id": "string",
                "sim_or_experiment": "simulation|experiment|not_reported",
                "experiment_level": "material_device|full_system|not_reported",
                "method": {"value": "string", "anchor_quote": "short exact phrase"},
                "results": {"value": "string", "anchor_quote": "short exact phrase"},
                "conditions": {"value": "string", "anchor_quote": "short exact phrase"},
                "limitations": {"value": "string", "anchor_quote": "short exact phrase",
                                "optional": True},
                "not_reported": ["field names the paper does not state"],
            }],
        },
    }
    return {"system_prompt": system, "user_payload": user}


def _sentence_span(text: str, start: int, end: int) -> Tuple[int, int]:
    import re as _re
    candidates = [text.rfind(". ", 0, start), text.rfind("! ", 0, start),
                  text.rfind("? ", 0, start), text.rfind("\n", 0, start)]
    head = max(candidates)
    tail_m = _re.search(r"[.!?](?:\s|$)", text[end:end + 400])
    tail = end + tail_m.end() if tail_m else min(len(text), end + 200)
    return max(0, head + 1), min(len(text), tail)


def _strip_json_fence(content: str) -> str:
    stripped = (content or "").strip()
    if stripped.startswith("```"):
        end = stripped.rfind("```")
        body = stripped[:end]
        body = body[body.find("\n") + 1:] if "\n" in body else body
        return body
    return stripped


def build_cards_from_response(response_content: str, *, document_id: str,
                              version_raw_hash: str, text: str,
                              permission_receipt: Optional[Dict[str, Any]] = None
                              ) -> Dict[str, Any]:
    """Local verification + card assembly.  Anchors are located verbatim in the
    canonical text and expanded to their sentence; every field references that
    span; unverifiable fields degrade to not_reported with a reason."""
    try:
        data = json.loads(_strip_json_fence(response_content))
    except ValueError:
        return {"status": "needs_source", "cards": [],
                "invalidation": "model_response_not_json"}
    cards: List[Dict[str, Any]] = []
    seen_experiment_ids = set()
    for exp in data.get("experiments") or []:
        if not isinstance(exp, dict):
            continue
        eid = str(exp.get("experiment_id") or "").strip()
        if not eid or eid in seen_experiment_ids:
            eid = f"exp{len(seen_experiment_ids) + 1}"
        seen_experiment_ids.add(eid)
        fields: Dict[str, Any] = {}
        for fname in _FIELDS:
            block = exp.get(fname)
            if not isinstance(block, dict):
                fields[fname] = {"status": "not_reported",
                                 "unknown_reason": "field_absent_in_response",
                                 "span_ids": []}
                continue
            anchor = str(block.get("anchor_quote") or block.get("quote") or "")
            value = str(block.get("value") or "")
            located = SR_locate(text, anchor)
            if located is None:
                fields[fname] = {"status": "not_reported", "value": "",
                                 "unknown_reason": "anchor_not_verbatim_in_source",
                                 "span_ids": [], "rejected_anchor": anchor[:120]}
                continue
            s_start, s_end = _sentence_span(text, located[0], located[1])
            sentence = text[s_start:s_end]
            span_id = "span_" + _sha(f"{document_id}|{sentence}")[:20]
            bad_number = None
            if value:
                for num in extract_numbers(value):
                    if num not in _norm(sentence):
                        bad_number = f"number_{num}_not_in_field_span"
                        break
            if bad_number:
                fields[fname] = {"status": "not_reported", "value": "",
                                 "unknown_reason": bad_number, "span_ids": [],
                                 "rejected_anchor": anchor[:120]}
                continue
            fields[fname] = {"status": "extracted", "value": value,
                             "span_ids": [span_id], "anchor_quote": anchor,
                             "field_span_text": sentence.strip(),
                             "field_span_offset": [s_start, s_end]}
        not_reported_names = exp.get("not_reported") or []
        for fname in not_reported_names:
            if fname in _FIELDS and fields.get(fname, {}).get("status") != "extracted":
                fields[fname] = {"status": "not_reported",
                                 "unknown_reason": "paper_does_not_state",
                                 "span_ids": []}
        card = {
            "schema_version": CARD_SCHEMA,
            "card_id": f"card_{_sha(document_id + '|' + eid)[:20]}",
            "document_id": document_id,
            "version_raw_hash": version_raw_hash,
            "experiment_id": eid,
            "sim_or_experiment": str(exp.get("sim_or_experiment") or "not_reported"),
            "experiment_level": str(exp.get("experiment_level") or "not_reported"),
            "fields": fields,
            "permission_receipt": permission_receipt,
            "status": "extracted",
        }
        cards.append(card)

    for card in cards:
        verified = sum(1 for f in card["fields"].values() if f["status"] == "extracted")
        card["status"] = "source_checked"
        card["usable_index"] = verified >= 2
        if verified == 0:
            card["status"] = "needs_source"
    return {"status": "extracted", "cards": cards}


def SR_locate(text: str, anchor: str) -> Optional[Tuple[int, int]]:
    """011-style unicode-folded verbatim location of a short anchor."""
    from optomind_research.runtime.upgrade3.source_resolver import locate_span
    return locate_span(text, anchor)


def invalidate_cards(cards: List[Dict[str, Any]],
                     current_raw_hash_by_doc: Dict[str, str]) -> List[str]:
    """Source hash change -> card stale (invalidation event ids returned)."""
    invalidated = []
    for card in cards:
        current = current_raw_hash_by_doc.get(card["document_id"])
        if current and current != card.get("version_raw_hash"):
            card["status"] = "needs_source"
            card["stale"] = True
            invalidated.append(card["card_id"])
    return invalidated


def extract_with_repair(chat_fn, *, document_id: str, version_raw_hash: str,
                        text: str, contract: Dict[str, Any],
                        max_chars: int = 6000, max_rounds: int = 3) -> Dict[str, Any]:
    """Extract cards with a bounded verification-repair loop: when fields fail
    verbatim checks (or no cards come back), re-ask once with the failed quotes
    attached so the model can copy exactly.  Unrepaired fields stay
    not_reported; nothing is padded."""
    paper = {"title": document_id, "text": text}
    prompt = build_judge_prompt(contract, paper, max_chars=max_chars)
    note = ""
    best = {"status": "needs_source", "cards": []}
    for round_no in range(max_rounds):
        user_payload = prompt["user_payload"]
        if note:
            user_payload = dict(user_payload, repair_note=note)
        response = chat_fn(system_prompt=prompt["system_prompt"],
                           user_payload=user_payload)
        built = build_cards_from_response(response["content"],
                                          document_id=document_id,
                                          version_raw_hash=version_raw_hash,
                                          text=text)
        failed_quotes = []
        for card in built["cards"]:
            for fname, f in card["fields"].items():
                if f["status"] == "not_reported" and f.get("rejected_quote"):
                    failed_quotes.append(f["rejected_quote"])
        if not built["cards"]:
            note = ("Your previous answer contained no experiments array. Return "
                    "the JSON object with an experiments array; copy quotes "
                    "character-for-character from paper_text.")
            best = built
            continue
        best = built
        if not failed_quotes:
            break
        note = ("These previous quotes were NOT verbatim in paper_text: "
                + json.dumps(failed_quotes[:6], ensure_ascii=False)
                + ". Copy quotes character-for-character from paper_text.")
    return best
