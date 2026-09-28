"""Upgrade-3 author workspace & submission contract (ticket 020).

The author sees exactly what it needs, structured:

- AUTHOR_WORKSPACE_MANIFEST records the selected claim ids, the visible span
  texts and conditions (claim+span+condition travel together — they are never
  split across pagination), token statistics per page, required handle
  coverage and the submission schema hash.  When a single argument block does
  not fit the budget the task is SHRUNK (status task_too_large) and a
  handle-read tool is offered, instead of silently truncating conditions.
- submission: exactly ONE canonical object form
  ({"paragraphs": [{"id", "claim_ids", "text"}]}).  The legacy R6 shapes — a
  JSON *string* argument_plan and a flat {claim_ids, text} object — are
  normalized locally ONCE; conflicting dual forms are rejected with the exact
  field name; unknown claim ids are rejected; empty paragraphs are rejected.
- the model only selects claims and writes prose; paper/chunk/permission are
  back-filled locally from the workspace.  A field error returns a minimal
  actionable repair note instead of re-asking the model to guess a format.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple
MANIFEST_SCHEMA = "optomind.upgrade3.author_workspace_manifest.v1"
CANONICAL_SCHEMA = {
    "type": "object",
    "properties": {
        "paragraphs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "claim_ids": {"type": "array", "items": {"type": "string"}},
                    "text": {"type": "string"},
                },
                "required": ["id", "claim_ids", "text"],
            },
        }
    },
    "required": ["paragraphs"],
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def token_estimate(text: str) -> int:
    return max(1, len(text) // 4)


class AuthorWorkspace:
    def __init__(self, *, section_id: str, generation_id: str,
                 claim_cards: Dict[str, Dict[str, Any]],
                 span_texts: Dict[str, str],
                 token_budget: int = 6000):
        self.section_id = section_id
        self.generation_id = generation_id
        self.claim_cards = claim_cards
        self.span_texts = span_texts
        self.token_budget = token_budget

    def paginate(self, blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """blocks: [{block_id, claim_ids, span_ids, conditions}] — indivisible.
        Necessary fields (claims, spans, conditions) stay visible; if one block
        alone exceeds the budget the task shrinks instead of truncating."""
        pages: List[Dict[str, Any]] = []
        current: Dict[str, Any] = {"page": 1, "blocks": [], "tokens": 0}
        page_no = 1
        task_too_large = None
        for block in blocks:
            span_text_combined = "".join(self.span_texts.get(s, "")
                                         for s in block.get("span_ids", []))
            block_tokens = token_estimate(json.dumps(block) + span_text_combined)
            if block_tokens > self.token_budget:
                task_too_large = {"block_id": block.get("block_id"),
                                  "block_tokens": block_tokens,
                                  "action": "shrink_task_or_use_handle_read_tool"}
                continue
            if current["tokens"] + block_tokens > self.token_budget:
                pages.append(current)
                page_no += 1
                current = {"page": page_no, "blocks": [], "tokens": 0}
            current["blocks"].append(block)
            current["tokens"] += block_tokens
        if current["blocks"]:
            pages.append(current)
        return {"pages": pages, "task_too_large": task_too_large}

    def manifest(self, blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
        selected_claims: List[str] = []
        visible_spans: Dict[str, str] = {}
        visible_conditions: Dict[str, List[str]] = {}
        for block in blocks:
            for cid in block.get("claim_ids", []):
                if cid not in selected_claims:
                    selected_claims.append(cid)
            for sid in block.get("span_ids", []):
                if sid in self.span_texts:
                    visible_spans[sid] = self.span_texts[sid]
            for cid, cond in (block.get("conditions") or {}).items():
                visible_conditions[cid] = cond
        covered_handles = set(selected_claims) & set(self.claim_cards.keys())
        manifest = {
            "schema_version": MANIFEST_SCHEMA,
            "section_id": self.section_id,
            "generation_id": self.generation_id,
            "selected_claim_ids": selected_claims,
            "visible_spans": visible_spans,
            "visible_conditions": visible_conditions,
            "pagination": self.paginate(blocks),
            "required_handle_coverage": {
                "required": sorted(self.claim_cards.keys()),
                "visible": sorted(covered_handles),
                "complete": set(self.claim_cards.keys()) <= covered_handles,
            },
            "schema_hash": _sha(_canonical(CANONICAL_SCHEMA)),
            "token_budget": self.token_budget,
        }
        manifest["manifest_hash"] = _sha(_canonical(
            {k: v for k, v in manifest.items() if k != "manifest_hash"}))
        return manifest


class SubmissionError(Exception):
    def __init__(self, field: str, message: str):
        super().__init__(f"{field}: {message}")
        self.field = field
        self.repair_note = f"fix field '{field}': {message}"


def normalize_submission(raw: Any, workspace: AuthorWorkspace
                         ) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """Local one-shot normalization: legacy JSON-string / flat forms accepted;
    dual-form conflicts and unknown ids rejected with exact field errors."""
    errors: List[Dict[str, str]] = []
    if not isinstance(raw, dict):
        return {}, [{"field": "root", "error": "submission must be an object"}]

    canonical = raw.get("paragraphs")
    legacy_string = raw.get("argument_plan")
    flat_claim_ids = raw.get("claim_ids")
    flat_text = raw.get("text")

    paragraphs = None
    if isinstance(canonical, list):
        paragraphs = canonical
        if legacy_string is not None or (flat_claim_ids is not None
                                         and flat_text is not None):
            errors.append({"field": "paragraphs",
                           "error": "dual_form_conflict: canonical paragraphs "
                                    "coexists with legacy argument_plan/flat form"})
    elif isinstance(legacy_string, str):
        # legacy R6 shape: a JSON string — safe local json.loads ONCE
        try:
            parsed = json.loads(legacy_string)
        except ValueError as exc:
            return {}, [{"field": "argument_plan",
                         "error": f"json_string_unparseable: {exc}"}]
        if isinstance(parsed, dict):
            paragraphs = parsed.get("paragraphs")
        elif isinstance(parsed, list):
            paragraphs = parsed
        else:
            return {}, [{"field": "argument_plan",
                         "error": "json_string_neither_object_nor_list"}]
    elif flat_claim_ids is not None and flat_text is not None:
        paragraphs = [{"id": "p1", "claim_ids": flat_claim_ids,
                       "text": flat_text}]
    if paragraphs is None:
        errors.append({"field": "paragraphs",
                       "error": "missing: expected canonical paragraphs "
                                "(or legacy argument_plan string / flat form)"})
        return {}, errors

    known = set(workspace.claim_cards.keys())
    checked: List[Dict[str, Any]] = []
    for i, p in enumerate(paragraphs):
        if not isinstance(p, dict):
            errors.append({"field": f"paragraphs[{i}]",
                           "error": "must be object"})
            continue
        pid = p.get("id") or f"p{i+1}"
        cids = p.get("claim_ids")
        text = p.get("text")
        if not cids:
            errors.append({"field": f"paragraphs[{i}].claim_ids",
                           "error": "empty_claim_ids"})
            continue
        if not text or not str(text).strip():
            errors.append({"field": f"paragraphs[{i}].text",
                           "error": "empty_text"})
            continue
        unknown = [c for c in cids if c not in known]
        if unknown:
            errors.append({"field": f"paragraphs[{i}].claim_ids",
                           "error": f"unknown_claim_id:{unknown}"})
            continue
        checked.append({"id": pid, "claim_ids": cids, "text": text})
    if errors:
        return {}, errors
    return {"paragraphs": checked}, errors


def validate_conditions(canonical: Dict[str, Any],
                        workspace: AuthorWorkspace) -> List[Dict[str, str]]:
    """Numbers written in a paragraph must be traceable to a referenced claim's
    visible span (same-experiment numbers) — cross-experiment mixing rejected."""
    errors = []
    span_text_all = " ".join(workspace.span_texts.values())
    for p in canonical.get("paragraphs", []):
        for cid in p["claim_ids"]:
            card = workspace.claim_cards.get(cid) or {}
            span_id = (card.get("field_span_ids") or [None])[0]
            span_text = workspace.span_texts.get(span_id, "") if span_id else ""
            for num in re_numbers(p["text"]):
                if span_text and num not in span_text and num in span_text_all:
                    errors.append({"field": f"paragraphs[{p['id']}].text",
                                   "error": f"cross_experiment_number:{num}"})
    return errors


def re_numbers(text: str) -> List[str]:
    import re as _re
    return _re.findall(r"\d+(?:\.\d+)?", text or "")


def _sha_alias(text: str) -> str:
    return _sha(text)
