"""M2a - Claim Decomposition for review blueprint sections.

Takes a blueprint section (with candidate_text_chunks and claim_graph_seed).
Production sections first build a merged candidate claim pool from bounded
evidence batches (advisory target 80-120 atomic claims), then Python selects
the bounded final claim set for the chapter draft.  Sections without an
evidence digest keep the legacy single bounded call so explicit 2-8 tests stay
possible and auditable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from llm.qwen_chat_client import call_qwen_chat
from optomind_research.claim_schema import (
    VALID_CLAIM_KINDS,
    VALID_EVIDENCE_TYPES,
    VALID_CLAIM_IMPORTANCE,
    Claim,
    infer_claim_kind_from_statement,
    validate_claim,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DECOMPOSER_PROMPT = PROJECT_ROOT / "prompts" / "Claim Decomposer.txt"

_MOCK_EVIDENCE_TYPES = ["mechanism", "measurement", "comparison", "application"]


# ---------------------------------------------------------------------------
# Claim-pool checkpoint helpers (atomic write + fingerprint-based resume)
# ---------------------------------------------------------------------------

def _compute_pool_fingerprint(
    section: dict[str, Any],
    batches: list[dict[str, Any]],
    claims_per_batch: int,
) -> str:
    """Return a short hex fingerprint over (section_id, batch_ids, config).

    Used to detect config changes that invalidate a saved checkpoint so that a
    stale checkpoint from a previous run with different batches is never
    silently re-used.
    """
    payload = {
        "section_id": section.get("section_id"),
        "batch_ids": [str(b.get("batch_id") or "") for b in batches],
        "claims_per_batch": claims_per_batch,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _write_claim_pool_checkpoint(
    path: Path,
    fingerprint: str,
    proposals: list[dict[str, Any]],
    batch_audit_rows: list[dict[str, Any]],
    proposal_counter: int,
    parsed_proposal_count: int,
    ungrounded_proposals_dropped: int,
    checkpoint_audit: dict[str, Any] | None = None,
) -> None:
    """Atomically persist the current batch-level checkpoint.

    Uses a sibling temp file + rename so a crash mid-write never leaves a
    partial JSON that poisons the next resume.  Failures are swallowed so a
    read-only filesystem never aborts the claim pool.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fingerprint": fingerprint,
            "proposals": proposals,
            "batch_audit_rows": batch_audit_rows,
            "proposal_counter": proposal_counter,
            "parsed_proposal_count": parsed_proposal_count,
            "ungrounded_proposals_dropped": ungrounded_proposals_dropped,
            "last_valid_audit": dict(checkpoint_audit or {}),
        }
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
        except Exception:
            os.unlink(tmp)
            raise
        os.replace(tmp, path)
    except Exception:
        # Checkpoint is best-effort; a filesystem error must never abort the pool.
        return


def _update_claim_pool_checkpoint_audit(
    path: Path | None,
    updates: dict[str, Any],
) -> None:
    """Atomically update audit-only fields without rewriting claim payloads."""

    if path is None or not updates:
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return
        audit = payload.get("last_valid_audit")
        if not isinstance(audit, dict):
            audit = {}
        audit.update(updates)
        payload["last_valid_audit"] = audit
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
        except Exception:
            os.unlink(tmp)
            raise
        os.replace(tmp, path)
    except Exception:
        # Audit persistence is best-effort and must not alter fail-open behavior.
        return


# ---------------------------------------------------------------------------
# Candidate claim-pool policy (section-level, advisory)
# ---------------------------------------------------------------------------

# A section's merged candidate claim pool has an advisory planning target of
# 80-120 atomic candidate claims.  This is a diagnostic/recommendation target,
# not a hard minimum or maximum: sparse evidence may yield fewer claims and
# rich evidence may yield more.  Python never pads and never truncates at the
# target boundary.
PREFERRED_CANDIDATE_CLAIM_POOL_RANGE: list[int] = [80, 120]
DEFAULT_CLAIM_POOL_BATCH_SIZE = 12
DEFAULT_CLAIM_POOL_CLAIMS_PER_BATCH = 8
DEFAULT_CLAIM_POOL_HTTP_TIMEOUT_SEC = 120.0

# The final claim selection is a downstream shortlist for planner/DAG
# compatibility only.  It is deliberately wider than the legacy 5-claim
# bottleneck so rich pools can inform the chapter, while the stored pool
# itself is never truncated.  Explicit caller limits remain supported.
DEFAULT_FINAL_CLAIM_SELECTION_LIMIT = 32


def advisory_claim_pool_status(count: int) -> str:
    """Classify a merged candidate claim count against the advisory range."""
    try:
        count = max(0, int(count))
    except (TypeError, ValueError):
        count = 0
    low, high = PREFERRED_CANDIDATE_CLAIM_POOL_RANGE
    if count < low:
        return "below_target_range"
    if count <= high:
        return "within_target_range"
    return "above_target_range"


_SEMANTIC_STOPWORDS = {
    "about",
    "above",
    "across",
    "after",
    "again",
    "against",
    "also",
    "although",
    "among",
    "and",
    "another",
    "because",
    "been",
    "before",
    "being",
    "between",
    "both",
    "but",
    "can",
    "cannot",
    "could",
    "does",
    "doing",
    "done",
    "during",
    "each",
    "either",
    "from",
    "further",
    "have",
    "having",
    "into",
    "itself",
    "many",
    "more",
    "most",
    "must",
    "need",
    "needs",
    "onto",
    "other",
    "over",
    "rather",
    "same",
    "should",
    "show",
    "shows",
    "shown",
    "such",
    "than",
    "that",
    "their",
    "then",
    "there",
    "these",
    "this",
    "those",
    "through",
    "under",
    "using",
    "when",
    "where",
    "which",
    "while",
    "with",
    "within",
    "without",
    "would",
    # Planning/prose words that are too generic to prove seed coverage.
    "claim",
    "claims",
    "evidence",
    "section",
    "review",
    "literature",
    "candidate",
    "planning",
    "based",
    "related",
    "requires",
    "require",
}


def _compact(value: Any, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _as_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _truncate_to_sentence(text: Any, max_chars: int = 800) -> str:
    """Truncate to a complete sentence boundary ≤ max_chars.

    Unlike _compact which slices raw characters, this preserves full
    sentences.  If no sentence terminal is found within max_chars, the text
    is returned up to max_chars with a trailing ellipsis so callers can
    detect the truncation and raise critic_flags.
    """
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(s) <= max_chars:
        return s
    window = s[:max_chars]
    for terminal in ("。", ".", "！", "!", "？", "?"):
        pos = window.rfind(terminal)
        if pos != -1:
            return window[: pos + 1].strip()
    # No sentence boundary found — return window with ellipsis marker
    return window.rstrip() + "…"


def _safe_json(text: str) -> dict:
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except Exception:
        m = re.search(r"\{.*\}", str(text or ""), re.S)
        if m:
            try:
                v = json.loads(m.group(0))
                return v if isinstance(v, dict) else {}
            except Exception:
                pass
    return {}


def _normalized_claim_key(statement: Any) -> tuple[str, tuple[str, ...]]:
    """Deterministic exact/normalized identity for a candidate claim.

    Returns (normalized text, ordered content tokens).  Both keys preserve
    word order, so paraphrases with the same content words merge while
    reversed or contradictory wordings do not.
    """
    text = re.sub(r"[^a-z0-9]+", " ", str(statement or "").lower()).strip()
    tokens = tuple(part for part in text.split() if part)
    return (text, tokens)


def _union_limited(*values: Any, limit: int = 8) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in _as_list(value):
            text = str(item or "").strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
            if len(out) >= limit:
                return out
    return out


def _merge_claim_proposal_entry(
    base: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    """Merge one duplicate candidate claim into another, preserving refs."""
    merged = dict(base)
    merged["statement"] = str(base.get("statement") or incoming.get("statement") or "")
    merged["evidence_type"] = str(
        base.get("evidence_type") or incoming.get("evidence_type") or "mechanism"
    )
    merged["claim_kind"] = str(
        base.get("claim_kind") or incoming.get("claim_kind")
        or infer_claim_kind_from_statement(merged["statement"])
    )
    merged["supporting_text_chunk_ids"] = _union_limited(
        base.get("supporting_text_chunk_ids"),
        incoming.get("supporting_text_chunk_ids"),
        limit=12,
    )
    for field in (
        "counterevidence_text_chunk_ids",
        "boundary_text_chunk_ids",
        "background_text_chunk_ids",
        "author_reported_support_chunk_ids",
    ):
        merged[field] = _union_limited(
            base.get(field), incoming.get(field), limit=6
        )
    merged["supporting_visual_chunk_ids"] = _union_limited(
        base.get("supporting_visual_chunk_ids"),
        incoming.get("supporting_visual_chunk_ids"),
        limit=4,
    )
    merged["relation_roles"] = _union_limited(
        base.get("relation_roles"), incoming.get("relation_roles"), limit=8
    )
    merged["boundary_conditions"] = _union_limited(
        base.get("boundary_conditions"), incoming.get("boundary_conditions"), limit=8
    )
    merged["axis_assignments"] = list(
        dict.fromkeys(
            json.dumps(item, sort_keys=True, ensure_ascii=False)
            for item in [
                *_as_list(base.get("axis_assignments")),
                *_as_list(incoming.get("axis_assignments")),
            ]
            if isinstance(item, dict)
        )
    )
    merged["axis_assignments"] = [
        json.loads(item) for item in merged["axis_assignments"]
    ][:12]
    try:
        merged["saturation_score"] = max(
            float(base.get("saturation_score") or 0.0),
            float(incoming.get("saturation_score") or 0.0),
        )
    except (TypeError, ValueError):
        merged["saturation_score"] = float(
            base.get("saturation_score") or incoming.get("saturation_score") or 0.0
        )
    merged["load_bearing"] = bool(
        base.get("load_bearing") or incoming.get("load_bearing")
    )
    merged["importance"] = (
        "load_bearing"
        if merged["load_bearing"]
        else str(base.get("importance") or incoming.get("importance") or "supporting")
    )
    merged["counterevidence_query"] = str(
        base.get("counterevidence_query")
        or incoming.get("counterevidence_query")
        or ""
    )
    merged["critic_flags"] = _union_limited(
        base.get("critic_flags"), incoming.get("critic_flags"), limit=12
    )
    merged_ids = _union_limited(
        base.get("merged_from_proposal_ids") or [base.get("claim_id")],
        incoming.get("merged_from_proposal_ids") or [incoming.get("claim_id")],
        limit=16,
    )
    merged["merged_from_proposal_ids"] = merged_ids
    if "merged_across_batches" not in merged["critic_flags"]:
        merged["critic_flags"].append("merged_across_batches")
    return merged


def merge_candidate_claim_proposals(
    entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Merge per-batch claim proposals by stable ID and normalized content.

    Duplicates are never concatenated blindly: supporting references, roles,
    and conditions are unioned deterministically while the statement is kept
    from the first proposal.  Returns (merged_entries, merge_audit).
    """
    by_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        proposal_id = str(
            entry.get("claim_proposal_id") or entry.get("claim_id") or ""
        ).strip()
        if not proposal_id:
            proposal_id = f"auto-{len(by_id) + 1:04d}"
        if proposal_id in by_id:
            by_id[proposal_id] = _merge_claim_proposal_entry(
                by_id[proposal_id], entry
            )
        else:
            by_id[proposal_id] = dict(entry)

    merged: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    merge_audit: dict[str, Any] = {
        "stable_id_duplicates": 0,
        "normalized_duplicates": 0,
        "duplicates_merged": 0,
        "merged_claim_ids": [],
    }
    for proposal_id in sorted(by_id):
        entry = by_id[proposal_id]
        key = _normalized_claim_key(entry.get("statement"))
        if key in merged:
            existing = _merge_claim_proposal_entry(merged[key], entry)
            merged[key] = existing
            merge_audit["duplicates_merged"] += 1
            merge_audit["merged_claim_ids"].append(proposal_id)
        else:
            merged[key] = dict(entry)
    merge_audit["stable_id_duplicates"] = max(
        0, len(entries) - len(by_id)
    )
    merge_audit["normalized_duplicates"] = max(
        0, len(by_id) - len(merged)
    )
    out = list(merged.values())
    out.sort(key=lambda item: str(item.get("claim_id") or ""))
    return out, merge_audit


def _section_claim_pool_batches(
    section: dict[str, Any],
    batch_size: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Return (batches, digest chunk index rows, source) for a section."""
    digest = section.get("candidate_evidence_digest")
    if isinstance(digest, dict):
        batches = [
            dict(item)
            for item in (digest.get("batches") or [])
            if isinstance(item, dict)
        ]
        chunk_index = digest.get("chunk_index") or []
        if batches:
            return batches, chunk_index, "evidence_digest"
    raw = (
        section.get("candidate_text_context")
        or section.get("candidate_text_chunks")
        or []
    )
    rows = [
        row for row in raw if isinstance(row, dict) and row.get("chunk_id")
    ]
    batches = []
    for offset in range(0, len(rows), batch_size):
        group = rows[offset : offset + batch_size]
        batches.append(
            {
                "batch_id": f"B{len(batches) + 1:02d}",
                "chunk_ids": [str(row["chunk_id"]) for row in group],
                "paper_ids": list(
                    dict.fromkeys(
                        str(row.get("paper_id") or "")
                        for row in group
                        if row.get("paper_id")
                    )
                ),
                "summary": _compact(
                    " ".join(
                        _compact(
                            row.get("summary")
                            or row.get("text_preview")
                            or row.get("text")
                            or "",
                            220,
                        )
                        for row in group
                    ),
                    900,
                ),
            }
        )
    return batches, rows, "raw_chunk_fallback"


def _semantic_stem(token: str) -> str:
    """Return a small deterministic stem for claim-seed coverage checks."""
    word = re.sub(r"[^a-z0-9]+", "", str(token or "").lower())
    if not word or word in _SEMANTIC_STOPWORDS:
        return ""
    for _ in range(3):
        original = word
        if len(word) > 6 and word.endswith("ization"):
            word = word[:-7] + "ize"
        elif len(word) > 6 and word.endswith("ational"):
            word = word[:-7] + "ate"
        elif len(word) > 5 and word.endswith("ical"):
            word = word[:-2]
        elif len(word) > 5 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 5 and word.endswith("ing"):
            word = word[:-3]
        elif len(word) > 5 and word.endswith("edly"):
            word = word[:-4]
        elif len(word) > 5 and word.endswith("ed"):
            word = word[:-2]
        elif len(word) > 5 and word.endswith("al"):
            word = word[:-2]
        elif len(word) > 4 and word.endswith("es"):
            word = word[:-2]
        elif len(word) > 4 and word.endswith("s"):
            word = word[:-1]
        elif len(word) > 4 and word.endswith("e"):
            word = word[:-1]
        if len(word) > 3 and len(word) >= 2 and word[-1] == word[-2]:
            word = word[:-1]
        if word == original:
            break
    if len(word) < 4 or word in _SEMANTIC_STOPWORDS:
        return ""
    return word


def semantic_claim_tokens(text: str) -> set[str]:
    """Tokenize scientific prose for conservative seed-to-claim coverage tests.

    This deliberately uses only deterministic normalization: stopword removal,
    short-token filtering, and light English morphology.  It is not meant to
    judge subtle paraphrase quality; it catches obvious missing planning
    directions without hard-coding application domains.
    """
    tokens: set[str] = set()
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9_\-]*|[0-9]+(?:\.[0-9]+)?", str(text or "").lower()):
        for part in re.split(r"[_\-]+", raw):
            stem = _semantic_stem(part)
            if stem:
                tokens.add(stem)
    return tokens


def _planning_claim_seed_texts(section: dict[str, Any]) -> list[str]:
    """Extract planning claim_seed strings from supported blueprint shapes."""
    graph = section.get("claim_graph_seed") if isinstance(section, dict) else {}
    if isinstance(graph, dict):
        raw = graph.get("central_claim_candidates") or []
    elif isinstance(graph, list):
        raw = graph
    else:
        raw = []
    out: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            text = _compact(item.get("claim_seed", ""), 420)
        else:
            text = _compact(item, 420)
        if text:
            out.append(text)
    return out


def _claim_statement_text(claim: Claim | dict[str, Any]) -> str:
    if isinstance(claim, Claim):
        return claim.statement
    if isinstance(claim, dict):
        return str(claim.get("statement") or claim.get("claim_seed") or "")
    return str(claim or "")


def _normalize_claim_importance(item: dict[str, Any], *, section_fit: str = "") -> str:
    raw = str(
        item.get("importance")
        or item.get("importance_level")
        or item.get("priority")
        or ("load_bearing" if item.get("load_bearing") else "supporting")
    ).strip().lower()
    if raw not in VALID_CLAIM_IMPORTANCE:
        raw = "supporting"
    if section_fit in {"boundary", "off_scope"} and raw == "load_bearing":
        raw = "supporting"
    return raw


def _compact_audit_provenance(row: dict[str, Any]) -> dict[str, Any]:
    """Expose only route facts needed by the claim generator."""
    raw = row.get("provenance") or row.get("route_provenance") or {}
    raw = raw if isinstance(raw, dict) else {}

    def pick(*keys: str, limit: int = 180) -> str:
        for key in keys:
            value = row.get(key)
            if value in (None, ""):
                value = raw.get(key)
            if value not in (None, ""):
                return _compact(value, limit)
        return ""

    values = {
        "provider": pick("provider", "source_provider"),
        "doi": pick("doi", "paper_doi"),
        "corpus_id": pick("corpus_id", "CorpusId", "s2_corpus_id"),
        "locator": pick("locator", "section_path", "source_url", "url", limit=220),
        "discovery_route": pick("discovery_route"),
        "materialization_route": pick("materialization_route"),
        "use_permission": pick("use_permission") or "discovery_only",
        "scope_fit": pick("scope_fit") or "unreviewed",
        "content_depth": pick("content_depth") or "metadata",
        "source_kind": pick("source_kind", "evidence_level") or "unknown",
    }
    return {key: value for key, value in values.items() if value not in ("", None)}


def audit_claim_seed_coverage(
    section_or_seed_texts: dict[str, Any] | list[str],
    claims: list[Claim] | list[dict[str, Any]],
) -> dict[str, Any]:
    """Audit whether every planning claim seed is covered by at least one claim.

    Coverage is intentionally conservative: a claim normally needs both a
    reasonable seed-token recall and at least one token that distinguishes that
    seed from sibling seeds.  A very high recall can still pass when wording is
    slightly redistributed across generic section vocabulary.
    """
    seed_texts = (
        _planning_claim_seed_texts(section_or_seed_texts)
        if isinstance(section_or_seed_texts, dict)
        else [_compact(x, 420) for x in section_or_seed_texts if _compact(x, 420)]
    )
    seed_tokens = [semantic_claim_tokens(text) for text in seed_texts]
    claim_tokens = [semantic_claim_tokens(_claim_statement_text(c)) for c in claims]
    unique_seed_tokens: list[set[str]] = []
    for idx, tokens in enumerate(seed_tokens):
        siblings = set().union(*(seed_tokens[j] for j in range(len(seed_tokens)) if j != idx))
        unique_seed_tokens.append(tokens - siblings)

    covered: list[int] = []
    missing: list[int] = []
    best_claim_indices: list[int | None] = []
    best_scores: list[float] = []
    details: list[dict[str, Any]] = []
    for idx, tokens in enumerate(seed_tokens):
        best_idx: int | None = None
        best_score = 0.0
        best_overlap: set[str] = set()
        best_anchor_overlap: set[str] = set()
        for cidx, ctokens in enumerate(claim_tokens):
            if not tokens or not ctokens:
                continue
            overlap = tokens & ctokens
            score = len(overlap) / max(1, len(tokens))
            if score > best_score:
                best_score = score
                best_idx = cidx
                best_overlap = overlap
                best_anchor_overlap = unique_seed_tokens[idx] & ctokens

        anchors = unique_seed_tokens[idx]
        if not tokens:
            is_covered = True
        elif len(tokens) <= 3:
            is_covered = best_score >= 0.67
        elif anchors:
            is_covered = (bool(best_anchor_overlap) and best_score >= 0.45) or best_score >= 0.78
        else:
            is_covered = best_score >= 0.60

        if is_covered:
            covered.append(idx)
        else:
            missing.append(idx)
        best_claim_indices.append(best_idx)
        best_scores.append(round(best_score, 3))
        details.append(
            {
                "seed_index": idx,
                "seed_text": seed_texts[idx],
                "covered": is_covered,
                "best_claim_index": best_idx,
                "best_recall": round(best_score, 3),
                "overlap_tokens": sorted(best_overlap),
                "seed_unique_tokens": sorted(anchors),
                "unique_overlap_tokens": sorted(best_anchor_overlap),
            }
        )

    return {
        "seed_count": len(seed_texts),
        "covered_seed_indices": covered,
        "missing_seed_indices": missing,
        "missing_seed_texts": [seed_texts[i] for i in missing],
        "best_claim_indices": best_claim_indices,
        "best_recall_scores": best_scores,
        "details": details,
    }


def rank_claim_set_quality(
    claims: list[Claim] | list[dict[str, Any]],
    section: dict[str, Any] | None = None,
) -> tuple[int, int, int, int, int, int, int, int, int]:
    """Return a quality tuple for choosing between complete claim-set candidates.

    Ordering is deliberate: planning seed coverage and section fit outrank
    evidence availability.  Unsupported but on-section claims remain valid M3
    gaps; well-supported but off-section claims should not win.
    """
    audit = audit_claim_seed_coverage(section, claims) if section else {
        "seed_count": 0,
        "covered_seed_indices": [],
        "missing_seed_indices": [],
    }
    seed_count = int(audit.get("seed_count") or 0)
    covered_count = len(audit.get("covered_seed_indices") or [])
    missing_count = len(audit.get("missing_seed_indices") or [])
    coverage_complete = int(seed_count == 0 or missing_count == 0)

    def get_value(claim: Claim | dict[str, Any], name: str, default: Any = "") -> Any:
        if isinstance(claim, Claim):
            return getattr(claim, name, default)
        if isinstance(claim, dict):
            return claim.get(name, default)
        return default

    core_fit = 0
    central_count = 0
    bad_fit_penalty = 0
    evidence_count = 0
    evidence_score = 0
    for claim in claims:
        section_fit = str(get_value(claim, "section_fit", "") or "").lower()
        if section_fit in {"central", "supporting"}:
            core_fit += 1
        if section_fit == "central":
            central_count += 1
        if section_fit == "boundary":
            bad_fit_penalty += 1
        elif section_fit == "off_scope":
            bad_fit_penalty += 3

        status = str(get_value(claim, "evidence_binding_status", "") or "").lower()
        text_ids = list(get_value(claim, "supporting_text_chunk_ids", []) or [])
        if status in {"direct", "synthesized", "partial"} and text_ids:
            evidence_count += 1
            evidence_score += (
                3 if status == "direct"
                else 2 if status == "synthesized"
                else 1
            )

    return (
        coverage_complete,
        covered_count,
        -missing_count,
        core_fit,
        central_count,
        -bad_fit_penalty,
        int(2 <= len(claims) <= 8),
        evidence_count,
        evidence_score,
    )


def build_claim_set_repair_instruction(
    *,
    missing_seed_texts: list[str] | None = None,
    rejected_statements: list[str] | None = None,
    seed_audit_stage: str = "seed coverage audit",
) -> str:
    """Build the complete-claim-set repair instruction used before/after verify."""
    rejected = list(rejected_statements or [])
    missing = list(missing_seed_texts or [])
    instruction = (
        "Regenerate a complete 2-8 claim set. Use only as many claims as the section contract and supplied evidence justify; never add filler claims. Every claim must perform the stated section argument role, "
        "not merely match the broad review topic. "
        f"Avoid these rejected or misplaced propositions: {json.dumps(rejected, ensure_ascii=False)}"
    )
    if missing:
        instruction += (
            f" The {seed_audit_stage} found required planning claim seeds that no generated claim clearly covers. "
            "Regenerate the complete claim set and preserve these missing required directions as claims: "
            f"{json.dumps(missing, ensure_ascii=False)}. "
            "Do not replace a missing required direction with a better-supported but off-direction claim. "
            "If no supplied text anchor supports a required direction, still include that direction as an explicit M3 gap claim with empty supporting_text_refs; "
            "do not attach irrelevant evidence just to avoid an empty reference list."
        )
    return instruction


VALID_VISUAL_ARGUMENT_TYPES = {
    "mechanism_anchor",
    "taxonomy_or_roadmap",
    "method_or_workflow",
    "quantitative_comparison",
    "trend_or_parameter_map",
    "representative_example",
    "anomaly_or_limitation",
    "synthesis_overview",
}


def _filter_ok_visual_chunks(
    visual_chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return only visual chunks with visual_argument_status == 'ok' and valid type."""
    out = []
    for vc in visual_chunks:
        if not isinstance(vc, dict):
            continue
        if vc.get("visual_argument_status") != "ok":
            continue
        if vc.get("visual_argument_type", "") not in VALID_VISUAL_ARGUMENT_TYPES:
            continue
        out.append(vc)
    return out


def _mock_claims(section: dict[str, Any]) -> list[Claim]:
    """Generate deterministic mock claims from claim_graph_seed for testing."""
    raw_seed = section.get("claim_graph_seed") or {}
    seeds = (
        raw_seed.get("central_claim_candidates") or []
        if isinstance(raw_seed, dict)
        else raw_seed if isinstance(raw_seed, list) else []
    )
    section_id = section.get("section_id", "S00")
    text_chunk_ids = list(section.get("candidate_text_chunk_ids") or [])

    claims: list[Claim] = []
    for idx in range(1, 5):
        seed = seeds[idx - 1] if idx - 1 < len(seeds) else {}
        if not isinstance(seed, dict):
            seed = {"claim_seed": seed}
        seed_text = seed.get("claim_seed", f"Claim {idx} for section {section_id}")
        seed_text_chunks = list(seed.get("supporting_text_chunk_ids") or [])
        chunk_support = seed_text_chunks or text_chunk_ids[:2]
        if not chunk_support and text_chunk_ids:
            chunk_support = [text_chunk_ids[0]]
        if not chunk_support:
            chunk_support = [f"mock_chunk_{section_id}_{idx}"]

        evidence_type = _MOCK_EVIDENCE_TYPES[(idx - 1) % len(_MOCK_EVIDENCE_TYPES)]
        saturation = round(0.8 + idx * 0.3, 1)
        load_bearing = idx <= 2
        stmt_text = re.sub(r"\s+", " ", str(seed_text or "")).strip()

        claims.append(
            Claim(
                claim_id=f"{section_id}-C{idx:02d}",
                statement=stmt_text,
                evidence_type=evidence_type,
                claim_kind=infer_claim_kind_from_statement(stmt_text),
                supporting_text_chunk_ids=chunk_support[:3],
                supporting_visual_chunk_ids=list(
                    seed.get("supporting_visual_chunk_ids") or []
                )[:2],
                saturation_score=min(saturation, 3.0),
                load_bearing=load_bearing,
                importance="load_bearing" if load_bearing else "supporting",
                relation_roles=[
                    str(value) for value in _as_list(seed.get("relation_roles"))
                    if str(value).strip()
                ],
                counterevidence_query=_compact(seed.get("counterevidence_query"), 420),
                boundary_conditions=[
                    _compact(value, 360)
                    for value in _as_list(seed.get("boundary_conditions"))
                    if _compact(value, 360)
                ][:6],
                axis_assignments=[
                    dict(value) for value in (seed.get("axis_assignments") or [])
                    if isinstance(value, dict)
                ][:6],
                counterevidence_text_chunk_ids=[
                    str(value) for value in _as_list(seed.get("counterevidence_text_chunk_ids"))
                    if str(value).strip()
                ][:4],
                boundary_text_chunk_ids=[
                    str(value) for value in _as_list(seed.get("boundary_text_chunk_ids"))
                    if str(value).strip()
                ][:4],
                background_text_chunk_ids=[
                    str(value) for value in _as_list(seed.get("background_text_chunk_ids"))
                    if str(value).strip()
                ][:4],
            )
        )
        if len(claims) >= 4:
            break

    # Ensure minimum 3 claims
    while len(claims) < 2:
        idx = len(claims) + 1
        chunk_support = text_chunk_ids[:2] or [f"mock_chunk_{section_id}_{idx}"]
        claims.append(
            Claim(
                claim_id=f"{section_id}-C{idx:02d}",
                statement=f"Evidence cluster for {section_id} requires further claim binding (stub {idx})",
                evidence_type="mechanism",
                supporting_text_chunk_ids=chunk_support,
                saturation_score=0.5,
                load_bearing=False,
                importance="supporting",
            )
        )
    return claims


def _parse_llm_claims(
    raw: dict,
    section_id: str,
    valid_chunk_ids: set[str],
    text_ref_map: dict[str, str] | None = None,
    valid_visual_ids: set[str] | None = None,
    visual_ref_map: dict[str, str] | None = None,
    *,
    claim_id_map: dict[str, str] | None = None,
    claim_id_slots: list[str] | None = None,
) -> list[Claim]:
    """Parse LLM JSON output into validated Claim objects."""
    raw_claims = raw.get("claims") or []
    if not isinstance(raw_claims, list):
        return []

    claims: list[Claim] = []
    used_slot_ids: set[str] = set()

    def _resolve_claim_id(item: dict[str, Any], fallback_index: int) -> str:
        if claim_id_map is not None:
            proposal_id = str(item.get("claim_proposal_id") or "").strip()
            if proposal_id in claim_id_map:
                return claim_id_map[proposal_id]
            for slot in (claim_id_slots or []):
                if slot not in used_slot_ids:
                    used_slot_ids.add(slot)
                    return slot
            return ""
        return f"{section_id}-C{fallback_index:02d}"

    def _map_text_refs_from_value(
        refs: Any,
        chunk_ids: Any,
        *,
        text_ref_map: dict[str, str] | None,
        valid_chunk_ids: set[str],
    ) -> list[str]:
        mapped_ids: list[str] = []
        for ref in _as_list(refs):
            mapped = (text_ref_map or {}).get(str(ref))
            if mapped and mapped in valid_chunk_ids:
                mapped_ids.append(mapped)
        for cid in _as_list(chunk_ids):
            if str(cid) in valid_chunk_ids:
                mapped_ids.append(str(cid))
        return list(dict.fromkeys(mapped_ids))[:4]

    for idx, item in enumerate(raw_claims[:8], start=1):
        if not isinstance(item, dict):
            continue
        # Preserve the complete model statement.  Preview fields may be
        # bounded for transport, but a claim itself is a semantic unit and
        # must never be cut at a character or word boundary.
        statement = re.sub(r"\s+", " ", str(item.get("statement", "") or "")).strip()
        if not statement:
            continue
        evidence_type = str(item.get("evidence_type", "mechanism")).lower()
        if evidence_type not in VALID_EVIDENCE_TYPES:
            evidence_type = "mechanism"

        claim_kind_raw = str(item.get("claim_kind", "")).lower().strip()
        if claim_kind_raw not in VALID_CLAIM_KINDS:
            claim_kind_raw = infer_claim_kind_from_statement(statement)

        # Show-then-pick uses short references (T01, T02...) so the model does
        # not need to copy opaque DOI-based IDs.  Legacy ID output remains
        # accepted, but every value is still checked against the candidate set.
        text_ids: list[str] = []
        for ref in _as_list(item.get("supporting_text_refs")):
            mapped = (text_ref_map or {}).get(str(ref))
            if mapped and mapped in valid_chunk_ids:
                text_ids.append(mapped)
        for cid in (item.get("supporting_text_chunk_ids") or []):
            if cid in valid_chunk_ids:
                text_ids.append(cid)
        text_ids = list(dict.fromkeys(text_ids))[:4]

        counterevidence_ids = _map_text_refs_from_value(
            [*_as_list(item.get("counterevidence_refs")), *_as_list(item.get("counterevidence_text_refs"))],
            _as_list(item.get("counterevidence_text_chunk_ids") or item.get("counterevidence_chunk_ids")),
            text_ref_map=text_ref_map,
            valid_chunk_ids=valid_chunk_ids,
        )
        boundary_ids = _map_text_refs_from_value(
            [*_as_list(item.get("boundary_refs")), *_as_list(item.get("boundary_text_refs")), *_as_list(item.get("qualification_refs"))],
            _as_list(item.get("boundary_text_chunk_ids") or item.get("boundary_chunk_ids") or item.get("qualification_chunk_ids")),
            text_ref_map=text_ref_map,
            valid_chunk_ids=valid_chunk_ids,
        )
        background_ids = _map_text_refs_from_value(
            [*_as_list(item.get("background_refs")), *_as_list(item.get("background_text_refs")), *_as_list(item.get("context_refs"))],
            _as_list(item.get("background_text_chunk_ids") or item.get("background_chunk_ids") or item.get("context_chunk_ids")),
            text_ref_map=text_ref_map,
            valid_chunk_ids=valid_chunk_ids,
        )
        author_reported_ids = _map_text_refs_from_value(
            [*_as_list(item.get("author_reported_support_refs")), *_as_list(item.get("author_reported_refs"))],
            _as_list(item.get("author_reported_support_chunk_ids") or item.get("author_reported_chunk_ids")),
            text_ref_map=text_ref_map,
            valid_chunk_ids=valid_chunk_ids,
        )
        raw_role_bindings = item.get("evidence_role_bindings") or []
        role_bindings: list[dict[str, Any]] = []
        if isinstance(raw_role_bindings, list):
            for raw_binding in raw_role_bindings[:8]:
                if not isinstance(raw_binding, dict):
                    continue
                role = str(raw_binding.get("role") or raw_binding.get("evidence_role") or "").strip()
                if not role:
                    continue
                refs = _map_text_refs_from_value(
                    raw_binding.get("text_refs") or raw_binding.get("refs") or [],
                    raw_binding.get("text_chunk_ids") or raw_binding.get("chunk_ids") or [],
                    text_ref_map=text_ref_map,
                    valid_chunk_ids=valid_chunk_ids,
                )
                role_bindings.append({
                    "role": role,
                    "text_chunk_ids": refs,
                    "components": [
                        _compact(value, 240)
                        for value in (raw_binding.get("components") or [])
                        if _compact(value, 240)
                    ][:6],
                })
        for binding in role_bindings:
            role = str(binding.get("role") or "").casefold()
            bound_ids = [str(value) for value in (binding.get("text_chunk_ids") or [])]
            if role in {"support", "positive_support", "direct_support"}:
                text_ids = list(dict.fromkeys([*text_ids, *bound_ids]))[:4]
            elif role in {"author_reported_support", "author_reported"}:
                author_reported_ids = list(dict.fromkeys([*author_reported_ids, *bound_ids]))[:4]
            elif role in {"counterevidence", "contradiction", "contrast"}:
                counterevidence_ids = list(dict.fromkeys([*counterevidence_ids, *bound_ids]))[:4]
            elif role in {"boundary", "boundary_condition", "qualification"}:
                boundary_ids = list(dict.fromkeys([*boundary_ids, *bound_ids]))[:4]
            elif role in {"background", "background_context", "context"}:
                background_ids = list(dict.fromkeys([*background_ids, *bound_ids]))[:4]
        critic_flags: list[str] = []
        if not text_ids:
            critic_flags.append("claim_unbound_after_generation")

        # Filter visual chunks: only accept IDs in valid_visual_ids (status==ok)
        raw_visual_ids: list[str] = []
        for ref in (item.get("supporting_visual_refs") or []):
            mapped = (visual_ref_map or {}).get(str(ref))
            if mapped:
                raw_visual_ids.append(mapped)
        raw_visual_ids.extend(str(x) for x in (item.get("supporting_visual_chunk_ids") or []))
        if valid_visual_ids is not None:
            visual_ids = [vid for vid in raw_visual_ids if vid in valid_visual_ids][:3]
        else:
            visual_ids = raw_visual_ids[:3]
        sat = float(item.get("saturation_score", 1.0))
        sat = max(0.0, min(3.0, sat))
        if not text_ids:
            sat = min(sat, 0.5)
        load_bearing = bool(item.get("load_bearing", False))
        importance = _normalize_claim_importance(item)
        load_bearing = importance == "load_bearing"

        # Infer claim_state from evidence binding fields provided by LLM
        ev_binding = str(item.get("evidence_binding_status", "")).lower().strip()
        if ev_binding in {"direct", "synthesized"} and text_ids:
            claim_state = "grounded"
        elif ev_binding == "partial" and text_ids:
            claim_state = "partially_grounded"
        elif ev_binding == "contradicted":
            claim_state = "contested"
        else:
            claim_state = "planned"

        claim_id = _resolve_claim_id(item, idx)
        if not claim_id:
            continue
        c = Claim(
            claim_id=claim_id,
            statement=statement,
            evidence_type=evidence_type,
            claim_kind=claim_kind_raw,
            claim_state=claim_state,
            supporting_text_chunk_ids=text_ids,
            supporting_visual_chunk_ids=visual_ids,
            saturation_score=sat,
            load_bearing=load_bearing,
            importance=importance,
            relation_roles=[
                str(value) for value in _as_list(item.get("relation_roles") or item.get("relation_hints"))
                if str(value).strip()
            ][:8],
            counterevidence_query=_compact(item.get("counterevidence_query"), 500),
            boundary_conditions=[
                _compact(value, 360)
                for value in _as_list(item.get("boundary_conditions"))
                if _compact(value, 360)
            ][:8],
            axis_assignments=[
                dict(value) for value in (item.get("axis_assignments") or [])
                if isinstance(value, dict)
            ][:8],
            counterevidence_text_chunk_ids=counterevidence_ids,
            boundary_text_chunk_ids=boundary_ids,
            background_text_chunk_ids=background_ids,
            author_reported_support_chunk_ids=author_reported_ids,
            evidence_role_bindings=role_bindings,
        )
        errs = validate_claim(c)
        # Isolate claims with structural statement errors — downgrade to
        # open_question gap claim rather than silently passing bad data.
        _stmt_errs = [e for e in errs if "incomplete" in e or "too short" in e or "missing terminal" in e]
        if _stmt_errs:
            c.claim_state = "open_question"
            c.load_bearing = False
            c.importance = "supporting"
            c.saturation_score = 0.0
            c.critic_flags = critic_flags + errs + ["isolated_incomplete_statement"]
        else:
            c.critic_flags = critic_flags + errs
        claims.append(c)

    if claims and not any(c.load_bearing for c in claims):
        # Never promote an incomplete/open question merely to satisfy a shape
        # invariant.  A section with no defensible backbone must remain visibly
        # under-specified so M3 or a human can repair it.
        promotable = next(
            (
                c for c in claims
                if c.claim_state != "open_question"
                and not any(
                    marker in flag
                    for flag in c.critic_flags
                    for marker in ("incomplete", "too short", "missing terminal")
                )
            ),
            None,
        )
        if promotable is not None:
            promotable.load_bearing = True
            promotable.importance = "load_bearing"
            promotable.critic_flags.append("load_bearing_added_by_postprocess")

    return claims


class ClaimDecomposer:
    """Decompose a blueprint section into final Claims plus a candidate pool."""

    _claim_pool_progress_lock = threading.Lock()

    def __init__(
        self,
        prompt_path: Path = DEFAULT_DECOMPOSER_PROMPT,
        model_tier: str = "standard_model",
        real_llm: bool = False,
        claim_pool_enabled: bool | None = None,
        claim_pool_batch_size: int | None = None,
        claim_pool_claims_per_batch: int = DEFAULT_CLAIM_POOL_CLAIMS_PER_BATCH,
        claim_pool_target_range: list[int] | None = None,
        final_claim_selection_limit: int = DEFAULT_FINAL_CLAIM_SELECTION_LIMIT,
        verify_candidate_pool_claims: bool = True,
        claim_pool_progress_path: Path | None = None,
        claim_pool_checkpoint_dir: Path | None = None,
    ) -> None:
        self.prompt_path = Path(prompt_path)
        self.model_tier = model_tier
        self.real_llm = real_llm
        # None = auto: enable the batched candidate claim pool whenever the
        # section carries an evidence digest (production sections do).  False
        # keeps the legacy bounded single-call path for explicit tests.
        self.claim_pool_enabled = claim_pool_enabled
        self.claim_pool_batch_size = max(
            4, int(claim_pool_batch_size or DEFAULT_CLAIM_POOL_BATCH_SIZE)
        )
        self.claim_pool_claims_per_batch = max(
            1,
            min(
                12,
                int(
                    claim_pool_claims_per_batch
                    or DEFAULT_CLAIM_POOL_CLAIMS_PER_BATCH
                ),
            ),
        )
        self.claim_pool_target_range = list(
            claim_pool_target_range or PREFERRED_CANDIDATE_CLAIM_POOL_RANGE
        )
        self.final_claim_selection_limit = max(
            1,
            int(
                final_claim_selection_limit
                or DEFAULT_FINAL_CLAIM_SELECTION_LIMIT
            ),
        )
        # Backward-compatible default: candidate-pool claims immediately enter
        # formal evidence verification.  Bulk candidate-pool planning sets
        # this False so the Qwen3.7-flash pool stage finishes and persists
        # before any premium/verifier work runs in a later explicit stage.
        self.verify_candidate_pool_claims = bool(verify_candidate_pool_claims)
        self.claim_pool_progress_path = (
            Path(claim_pool_progress_path)
            if claim_pool_progress_path is not None
            else None
        )
        # Derive a default checkpoint dir from the progress path when the caller
        # does not supply one explicitly, so existing call-sites get checkpointing
        # without any code change.
        self.claim_pool_checkpoint_dir: Path | None = (
            Path(claim_pool_checkpoint_dir)
            if claim_pool_checkpoint_dir is not None
            else (
                self.claim_pool_progress_path.parent / "claim_pool_last_valid"
                if self.claim_pool_progress_path is not None
                else None
            )
        )
        self._system_prompt: str | None = None
        self.last_audit: dict[str, Any] = {}
        self.last_input_payload: dict[str, Any] = {}

    @classmethod
    def _append_claim_pool_progress(
        cls,
        path: Path | None,
        event: dict[str, Any],
    ) -> None:
        """Append a bounded observability checkpoint without affecting logic."""

        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with cls._claim_pool_progress_lock:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:
            # Progress observability is best-effort. A read-only output dir or
            # transient file error must not change claim-pool fail-open/closed
            # behaviour.
            return

    def _load_prompt(self) -> str:
        if self._system_prompt is None:
            self._system_prompt = self.prompt_path.read_text(encoding="utf-8").strip()
        return self._system_prompt

    def _claim_pool_active(self, section: dict[str, Any]) -> bool:
        """Decide whether the batched candidate claim pool runs."""
        if self.claim_pool_enabled is False:
            return False
        if self.claim_pool_enabled is True:
            return True
        digest = section.get("candidate_evidence_digest")
        return bool(
            isinstance(digest, dict) and (digest.get("batches") or [])
        )

    def _empty_candidate_claim_pool(
        self, section: dict[str, Any], *, reason: str
    ) -> dict[str, Any]:
        audit = {
            "schema_version": "review_blueprint.candidate_claim_pool_audit.v1",
            "section_id": str(section.get("section_id") or ""),
            "target_range": list(self.claim_pool_target_range),
            "pool_status": "no_pool",
            "reason": reason,
            "advisory_only": True,
            "batch_count": 0,
            "batch_size": self.claim_pool_batch_size,
            "claims_per_batch_limit": self.claim_pool_claims_per_batch,
            "chunks_total": 0,
            "chunks_covered": 0,
            "missing_chunk_ids": [],
            "chunk_to_batch": {},
            "proposals_requested": 0,
            "proposals_returned": 0,
            "claims_after_merge": 0,
            "duplicates_merged": 0,
            "merged_claim_ids": [],
            "padded": False,
            "explicit_limits": {
                "claim_pool_enabled": self.claim_pool_enabled,
                "batch_size": self.claim_pool_batch_size,
                "claims_per_batch_limit": self.claim_pool_claims_per_batch,
                "final_selection_limit": self.final_claim_selection_limit,
            },
        }
        return {
            "schema_version": "review_blueprint.candidate_claim_pool.v1",
            "section_id": str(section.get("section_id") or ""),
            "target_range": list(self.claim_pool_target_range),
            "pool_status": "no_pool",
            "claims": [],
            "batches": [],
            "audit": audit,
        }

    def _build_claim_pool_batch_payload(
        self,
        section: dict[str, Any],
        batch: dict[str, Any],
        batch_rows: list[dict[str, Any]],
        slots: list[str],
        batch_index: int,
        batch_count: int,
    ) -> dict[str, Any]:
        base = self._build_input_payload(section)
        chunk_rows = []
        for idx, row in enumerate(batch_rows, start=1):
            if not isinstance(row, dict) or not row.get("chunk_id"):
                continue
            chunk_rows.append(
                {
                    "ref": f"T{idx:02d}",
                    "chunk_id": str(row.get("chunk_id") or ""),
                    "summary": _compact(
                        row.get("summary")
                        or row.get("text_preview")
                        or row.get("text")
                        or "",
                        260,
                    ),
                    "title": _compact(row.get("title"), 100),
                    "paper_id": str(row.get("paper_id") or ""),
                    "use_permission": row.get(
                        "use_permission", "discovery_only"
                    ),
                    "source_kind": row.get("source_kind", "unknown"),
                    "content_depth": row.get("content_depth", "metadata"),
                }
            )
        return {
            "task": "claim_proposal_batch",
            "section_id": base["section_id"],
            "section_title": base["section_title"],
            "argument_role": base["argument_role"],
            "section_contract": base["section_contract"],
            "review_mentor_advice": base["review_mentor_advice"],
            "claim_seeds": base["claim_seeds"],
            "claim_seed_contracts": base["claim_seed_contracts"],
            "candidate_text_model_policy": base[
                "candidate_text_model_policy"
            ],
            "candidate_visual_chunks": base["candidate_visual_chunks"],
            "batch": {
                "batch_id": str(batch.get("batch_id") or ""),
                "batch_index": batch_index,
                "batch_count": batch_count,
                "chunk_ids": [
                    str(cid) for cid in (batch.get("chunk_ids") or [])
                ],
                "paper_ids": list(batch.get("paper_ids") or []),
                "summary": _compact(batch.get("summary"), 900),
                "chunk_index": chunk_rows,
                "claim_proposal_ids": list(slots),
            },
            "claim_pool_policy": {
                "target_range": list(self.claim_pool_target_range),
                "advisory_only": True,
                "claims_per_batch_max": self.claim_pool_claims_per_batch,
                "stable_id_rule": (
                    "Return only claim_proposal_ids supplied for this batch."
                ),
                "padding_forbidden": True,
            },
        }

    def build_candidate_claim_pool(
        self, section: dict[str, Any]
    ) -> dict[str, Any]:
        """Build the merged candidate claim pool for one section.

        Every retained candidate chunk is assigned to exactly one bounded
        batch (from the evidence digest or a deterministic raw-chunk fallback).
        Each batch LLM call may return a modest number of atomic claim
        proposals keyed by Python-supplied stable IDs; Python then merges
        cross-batch duplicates and produces the audit.
        """
        section_id = str(section.get("section_id") or "S00")
        self.last_audit.setdefault("claim_pool_generation_attempts", [])
        batches, digest_index, batch_source = _section_claim_pool_batches(
            section, self.claim_pool_batch_size
        )
        if not batches:
            return self._empty_candidate_claim_pool(
                section, reason="no_batches"
            )
        retained_ids = list(
            dict.fromkeys(
                str(cid)
                for batch in batches
                for cid in (batch.get("chunk_ids") or [])
            )
        )
        valid_text_ids = set(retained_ids) | {
            str(cid)
            for cid in (section.get("candidate_text_chunk_ids") or [])
        }
        chunk_to_batch = {
            str(cid): str(batch.get("batch_id") or "")
            for batch in batches
            for cid in (batch.get("chunk_ids") or [])
        }
        index_by_id = {
            str(row.get("chunk_id") or ""): row
            for row in digest_index
            if isinstance(row, dict) and row.get("chunk_id")
        }
        ok_visual = _filter_ok_visual_chunks(
            section.get("candidate_visual_chunks") or []
        )
        valid_visual_ids = {
            str(v.get("chunk_id") or "") for v in ok_visual
        }
        visual_ref_map = {
            f"V{idx:02d}": str(v.get("chunk_id") or "")
            for idx, v in enumerate(ok_visual[:10], start=1)
        }
        all_proposals: list[dict[str, Any]] = []
        proposal_counter = 0
        batch_audit_rows: list[dict[str, Any]] = []
        attempts: list[dict[str, Any]] = []
        parsed_proposal_count = 0
        ungrounded_proposals_dropped = 0
        section_aborted = False
        abort_reason = ""
        abort_error = ""
        claim_pool_timeout_seconds = float(
            os.environ.get(
                "QWEN_CLAIM_POOL_HTTP_TIMEOUT_SEC",
                DEFAULT_CLAIM_POOL_HTTP_TIMEOUT_SEC,
            )
        )
        # --- Fingerprint + checkpoint resume ---
        _pool_fingerprint = _compute_pool_fingerprint(
            section, batches, self.claim_pool_claims_per_batch
        )
        _checkpoint_path: Path | None = (
            self.claim_pool_checkpoint_dir / f"{section_id}.json"
            if self.claim_pool_checkpoint_dir is not None
            else None
        )
        self.last_audit["claim_pool_checkpoint_path"] = (
            str(_checkpoint_path) if _checkpoint_path is not None else ""
        )
        self.last_audit["claim_pool_fingerprint"] = _pool_fingerprint
        _completed_batch_ids: set[str] = set()
        if _checkpoint_path is not None and _checkpoint_path.exists():
            try:
                _ckpt = json.loads(
                    _checkpoint_path.read_text(encoding="utf-8")
                )
                if _ckpt.get("fingerprint") == _pool_fingerprint:
                    all_proposals = list(_ckpt.get("proposals") or [])
                    batch_audit_rows = list(
                        _ckpt.get("batch_audit_rows") or []
                    )
                    proposal_counter = int(
                        _ckpt.get("proposal_counter") or 0
                    )
                    parsed_proposal_count = int(
                        _ckpt.get("parsed_proposal_count") or 0
                    )
                    ungrounded_proposals_dropped = int(
                        _ckpt.get("ungrounded_proposals_dropped") or 0
                    )
                    _completed_batch_ids = {
                        str(r.get("batch_id") or "") for r in batch_audit_rows
                    }
            except Exception:
                pass  # Corrupt or stale checkpoint — start fresh
        # --- End fingerprint + checkpoint resume ---
        self._append_claim_pool_progress(
            self.claim_pool_progress_path,
            {
                "event": "claim_pool_started",
                "section_id": section_id,
                "planned_batch_count": len(batches),
                "batch_size": self.claim_pool_batch_size,
                "claims_per_batch_limit": self.claim_pool_claims_per_batch,
                "timeout_seconds": claim_pool_timeout_seconds,
                "batch_source": batch_source,
                "resumed_batch_count": len(_completed_batch_ids),
                "ts": time.time(),
            },
        )
        for batch_index, batch in enumerate(batches, start=1):
            batch_id = str(batch.get("batch_id") or "")
            if batch_id and batch_id in _completed_batch_ids:
                continue  # Already completed in a prior run; skip
            batch_chunk_ids = [
                str(cid) for cid in (batch.get("chunk_ids") or [])
            ]
            batch_rows = [
                index_by_id[cid] for cid in batch_chunk_ids if cid in index_by_id
            ]
            text_ref_map = {
                f"T{idx:02d}": str(row.get("chunk_id") or "")
                for idx, row in enumerate(batch_rows, start=1)
            }
            slots = [
                f"{section_id}-P{proposal_counter + idx:03d}"
                for idx in range(1, self.claim_pool_claims_per_batch + 1)
            ]
            proposal_counter += len(slots)
            payload = self._build_claim_pool_batch_payload(
                section,
                batch,
                batch_rows,
                slots,
                batch_index,
                len(batches),
            )
            estimated_input_tokens = max(
                1, len(json.dumps(payload, ensure_ascii=False)) // 4
            )
            batch_started_at = time.monotonic()
            self._append_claim_pool_progress(
                self.claim_pool_progress_path,
                {
                    "event": "batch_started",
                    "section_id": section_id,
                    "batch_id": batch_id,
                    "batch_index": batch_index,
                    "batch_count": len(batches),
                    "estimated_input_tokens": estimated_input_tokens,
                    "ts": time.time(),
                },
            )
            try:
                result = call_qwen_chat(
                    "ClaimDecomposerAgent",
                    [
                        {"role": "system", "content": self._load_prompt()},
                        {
                            "role": "user",
                            "content": json.dumps(
                                payload, ensure_ascii=False
                            ),
                        },
                    ],
                    model_tier=self.model_tier,
                    temperature=0,
                    max_tokens=4200,
                    response_format={"type": "json_object"},
                    stream=True,
                    accept_partial_stream=False,
                    force_mock=False,
                    max_retries=1,
                    allow_model_fallback=False,
                    max_key_candidates=1,
                    max_transport_key_candidates=1,
                    timeout_seconds=claim_pool_timeout_seconds,
                    enable_thinking=False,
                )
            except Exception as exc:
                attempts.append(
                    {
                        "batch_id": str(batch.get("batch_id") or ""),
                        "model": self.model_tier,
                        "max_retries": 1,
                        "failed": True,
                        "error": type(exc).__name__,
                        "estimated_input_tokens": estimated_input_tokens,
                    }
                )
                self.last_audit["claim_pool_generation_attempts"].append(
                    attempts[-1]
                )
                section_aborted = True
                abort_reason = "batch_exception"
                abort_error = type(exc).__name__
                self._append_claim_pool_progress(
                    self.claim_pool_progress_path,
                    {
                        "event": "batch_finished",
                        "section_id": section_id,
                        "batch_id": batch_id,
                        "batch_index": batch_index,
                        "elapsed_seconds": round(
                            time.monotonic() - batch_started_at, 3
                        ),
                        "success": False,
                        "error_type": abort_error,
                        "ts": time.time(),
                    },
                )
                break
            usage = result.get("_llm_usage")
            if not isinstance(usage, dict):
                usage = {}
            raw = str(result.get("content") or "")
            failed = (
                not bool(usage.get("success"))
                or bool(result.get("error") or result.get("failure"))
                or not raw.strip()
            )
            attempts.append(
                {
                    "batch_id": str(batch.get("batch_id") or ""),
                    "model": self.model_tier,
                    "max_tokens": 4200,
                    "estimated_input_tokens": estimated_input_tokens,
                    "estimated_output_tokens": max(1, len(raw) // 4),
                    "raw_chars": len(raw),
                    "usage": dict(usage),
                    "usage_recorded": bool(usage),
                    "retries": int(usage.get("retry_count") or 0),
                    "max_retries": 1,
                    "failed": failed,
                }
            )
            self.last_audit["claim_pool_generation_attempts"].append(
                attempts[-1]
            )
            self._append_claim_pool_progress(
                self.claim_pool_progress_path,
                {
                    "event": "batch_finished",
                    "section_id": section_id,
                    "batch_id": batch_id,
                    "batch_index": batch_index,
                    "elapsed_seconds": round(
                        time.monotonic() - batch_started_at, 3
                    ),
                    "success": not failed,
                    "error_type": abort_error if failed else "",
                    "ts": time.time(),
                },
            )
            if failed:
                section_aborted = True
                abort_reason = "batch_transport_failure"
                abort_error = str(
                    usage.get("error_type")
                    or usage.get("error")
                    or result.get("error")
                    or "provider_failure"
                )
                break
            parsed = _safe_json(raw)
            parsed_claims = _parse_llm_claims(
                parsed,
                section_id,
                valid_text_ids,
                text_ref_map,
                valid_visual_ids,
                visual_ref_map,
                claim_id_map={slot: slot for slot in slots},
                claim_id_slots=slots,
            )
            parsed_proposal_count += len(parsed_claims)
            grounded_claims = [
                claim
                for claim in parsed_claims
                if any(
                    (
                        claim.supporting_text_chunk_ids,
                        claim.counterevidence_text_chunk_ids,
                        claim.boundary_text_chunk_ids,
                        claim.background_text_chunk_ids,
                        claim.author_reported_support_chunk_ids,
                    )
                )
            ]
            ungrounded_proposals_dropped += (
                len(parsed_claims) - len(grounded_claims)
            )
            for claim in grounded_claims:
                entry = claim.to_dict()
                entry["source_batch_id"] = str(batch.get("batch_id") or "")
                entry["claim_proposal_id"] = claim.claim_id
                entry["merged_from_proposal_ids"] = [claim.claim_id]
                all_proposals.append(entry)
            batch_audit_rows.append(
                {
                    "batch_id": str(batch.get("batch_id") or ""),
                    "chunk_ids": batch_chunk_ids,
                    "paper_ids": list(batch.get("paper_ids") or []),
                    "parsed_claim_count": len(parsed_claims),
                    "claim_count": len(grounded_claims),
                    "ungrounded_proposals_dropped": (
                        len(parsed_claims) - len(grounded_claims)
                    ),
                    "proposal_slots": list(slots),
                }
            )
            # Atomically persist checkpoint so a restart can skip this batch.
            if _checkpoint_path is not None:
                _write_claim_pool_checkpoint(
                    _checkpoint_path,
                    _pool_fingerprint,
                    all_proposals,
                    batch_audit_rows,
                    proposal_counter,
                    parsed_proposal_count,
                    ungrounded_proposals_dropped,
                )

        merged_claims, merge_audit = merge_candidate_claim_proposals(
            all_proposals
        )
        # covered_ids must reflect chunks from batches that actually ran to
        # completion (batch_audit_rows), not the full planned set.
        # chunk_to_batch is built upfront from all batches; on a mid-pool
        # abort it includes chunks whose batches never executed, which
        # falsely inflates chunks_covered and masks genuine gaps.
        covered_ids = {
            str(cid)
            for row in batch_audit_rows
            for cid in (row.get("chunk_ids") or [])
        }
        missing_chunk_ids = sorted(set(retained_ids) - covered_ids)
        low, high = self.claim_pool_target_range
        pool_status = (
            "aborted"
            if section_aborted
            else advisory_claim_pool_status(len(merged_claims))
        )
        audit = {
            "schema_version": "review_blueprint.candidate_claim_pool_audit.v1",
            "section_id": section_id,
            "target_range": list(self.claim_pool_target_range),
            "pool_status": pool_status,
            "advisory_only": True,
            "batch_count": len(batches),
            "batch_size": self.claim_pool_batch_size,
            "claims_per_batch_limit": self.claim_pool_claims_per_batch,
            "chunks_total": len(retained_ids),
            "chunks_covered": len(covered_ids),
            "missing_chunk_ids": missing_chunk_ids,
            "chunk_to_batch": chunk_to_batch,
            "batch_source": batch_source,
            "proposals_requested": proposal_counter,
            "proposals_parsed": parsed_proposal_count,
            "proposals_returned": len(all_proposals),
            "ungrounded_proposals_dropped": ungrounded_proposals_dropped,
            "claims_after_merge": len(merged_claims),
            "duplicates_merged": merge_audit["duplicates_merged"],
            "merged_claim_ids": merge_audit["merged_claim_ids"],
            "stored_pool_count": len(merged_claims),
            "section_aborted": section_aborted,
            "abort_reason": abort_reason,
            "abort_error": abort_error,
            "batches_completed_before_abort": len(batch_audit_rows),
            "completed_batch_count": len(batch_audit_rows),
            "failed_batch_id": next(
                (
                    str(batch.get("batch_id") or "")
                    for batch in batches
                    if str(batch.get("batch_id") or "")
                    not in {
                        str(row.get("batch_id") or "")
                        for row in batch_audit_rows
                    }
                ),
                "",
            ),
            "final_selection_limit": self.final_claim_selection_limit,
            "final_selection_policy": (
                "downstream_shortlist_only_stored_pool_never_truncated"
            ),
            "padded": False,
            "padding_policy": (
                "Never pad to reach the target; sparse evidence yields fewer "
                "claims and rich evidence is retained in full."
            ),
            "batches": batch_audit_rows,
            "attempts": attempts,
            "explicit_limits": {
                "claim_pool_enabled": self.claim_pool_enabled,
                "batch_size": self.claim_pool_batch_size,
                "claims_per_batch_limit": self.claim_pool_claims_per_batch,
                "final_selection_limit": self.final_claim_selection_limit,
            },
        }
        self.last_audit["candidate_claim_pool_audit"] = audit
        _write_claim_pool_checkpoint(
            _checkpoint_path,
            _pool_fingerprint,
            all_proposals,
            batch_audit_rows,
            proposal_counter,
            parsed_proposal_count,
            ungrounded_proposals_dropped,
            checkpoint_audit={
                "completed_batch_count": len(batch_audit_rows),
                "failed_batch_id": str(audit.get("failed_batch_id") or ""),
                "abort_reason": abort_reason,
                "abort_error": abort_error,
                "selected_count": 0,
            },
        )
        self._append_claim_pool_progress(
            self.claim_pool_progress_path,
            {
                "event": "claim_pool_finished",
                "section_id": section_id,
                "status": "aborted" if section_aborted else "completed",
                "planned_batch_count": len(batches),
                "completed_batch_count": len(batch_audit_rows),
                "claims_after_merge": len(merged_claims),
                "ts": time.time(),
            },
        )
        return {
            "schema_version": "review_blueprint.candidate_claim_pool.v1",
            "section_id": section_id,
            "target_range": list(self.claim_pool_target_range),
            "pool_status": pool_status,
            "claims": merged_claims,
            "batches": batch_audit_rows,
            "audit": audit,
        }

    def _select_final_claims_from_pool(
        self,
        pool: dict[str, Any],
        section: dict[str, Any],
    ) -> list[Claim]:
        """Deterministically select the bounded final claim set from the pool.

        Selection prioritizes load-bearing proposals, planning-seed coverage,
        saturation, and batch diversity.  It never dumps the full candidate
        pool into the chapter draft.
        """
        entries = list(pool.get("claims") or [])
        if not entries:
            return []
        batch_order = [
            str(row.get("batch_id") or "")
            for row in (pool.get("batches") or [])
        ]
        scored: list[tuple[Any, ...]] = []
        for entry in entries:
            seed_audit = audit_claim_seed_coverage(section, [entry])
            covered_count = len(seed_audit.get("covered_seed_indices") or [])
            seed_count = int(seed_audit.get("seed_count") or 0)
            batch_id = str(entry.get("source_batch_id") or "")
            batch_index = (
                batch_order.index(batch_id) if batch_id in batch_order else 0
            )
            try:
                saturation = float(entry.get("saturation_score") or 0.0)
            except (TypeError, ValueError):
                saturation = 0.0
            scored.append(
                (
                    bool(entry.get("load_bearing")),
                    covered_count,
                    seed_count,
                    saturation,
                    -batch_index,
                    str(entry.get("claim_id") or ""),
                    entry,
                )
            )
        scored.sort(key=lambda row: row[:-1], reverse=True)
        ranked_entries = [row[-1] for row in scored]
        selected: list[dict[str, Any]] = []
        deferred_near_duplicates: list[dict[str, Any]] = []
        selected_token_sets: list[set[str]] = []

        def near_duplicate(tokens: set[str]) -> bool:
            if not tokens:
                return False
            for existing in selected_token_sets:
                overlap = tokens & existing
                if not overlap:
                    continue
                jaccard = len(overlap) / max(1, len(tokens | existing))
                containment = max(
                    len(overlap) / max(1, len(tokens)),
                    len(overlap) / max(1, len(existing)),
                )
                if max(jaccard, containment) >= 0.72:
                    return True
            return False

        for entry in ranked_entries:
            tokens = semantic_claim_tokens(entry.get("statement", ""))
            if near_duplicate(tokens):
                deferred_near_duplicates.append(entry)
                continue
            selected.append(entry)
            selected_token_sets.append(tokens)
            if len(selected) >= self.final_claim_selection_limit:
                break
        # Novelty is a ranking preference, not a destructive gate.  A sparse
        # section may legitimately need closely related claims to reach its
        # evidence-supported shortlist.
        if len(selected) < self.final_claim_selection_limit:
            selected.extend(
                deferred_near_duplicates[
                    : self.final_claim_selection_limit - len(selected)
                ]
            )
        self.last_audit["candidate_claim_shortlist_diversity"] = {
            "policy": "novelty_first_then_fill_without_deletion",
            "similarity_threshold": 0.72,
            "near_duplicate_candidates_deferred": len(
                deferred_near_duplicates
            ),
            "selected_before_seed_repair": len(selected),
        }
        selected_ids = {str(e.get("claim_id") or "") for e in selected}
        seed_audit = audit_claim_seed_coverage(section, selected)
        missing_seed_indices = set(seed_audit.get("missing_seed_indices") or [])
        if missing_seed_indices:
            for entry in entries:
                if str(entry.get("claim_id") or "") in selected_ids:
                    continue
                entry_audit = audit_claim_seed_coverage(section, [entry])
                entry_covered = set(
                    entry_audit.get("covered_seed_indices") or []
                )
                if entry_covered & missing_seed_indices:
                    selected.append(entry)
                    selected_ids.add(str(entry.get("claim_id") or ""))
                    missing_seed_indices -= entry_covered
                    if (
                        len(selected) >= self.final_claim_selection_limit
                        or not missing_seed_indices
                    ):
                        break
        selected = selected[: self.final_claim_selection_limit]
        return [Claim.from_dict(dict(entry)) for entry in selected]

    def decompose_section(self, section: dict[str, Any]) -> list[Claim]:
        """Return final Claim objects for a blueprint section.

        When the batched candidate claim pool is active, the pool is attached
        to ``section["candidate_claim_pool"]`` and the returned claims are the
        bounded deterministic selection from that pool.
        """
        if not self.real_llm:
            return _mock_claims(section)
        return self._llm_decompose(section)

    def _build_input_payload(self, section: dict[str, Any]) -> dict[str, Any]:
        raw_contract = section.get("section_contract") or section.get("section_argument_contract") or {}
        contract = dict(raw_contract) if isinstance(raw_contract, dict) else {}
        try:
            contract_word_budget = max(0, int(contract.get("word_budget") or 0))
        except (TypeError, ValueError):
            contract_word_budget = 0
        # Keep the full candidate pool for exact binding, but send the model a
        # digest assembled from material-card propositions and small batches.
        # This prevents a large chunk raw prompt from drowning the claim
        # decomposition step while preserving every chunk as a reopenable ID.
        raw_text_chunks = section.get("candidate_text_context") or section.get("candidate_text_chunks") or []
        digest = section.get("candidate_evidence_digest")
        digest_index = digest.get("chunk_index") if isinstance(digest, dict) else []
        if isinstance(digest_index, list) and digest_index:
            chunk_previews = []
            for idx, item in enumerate(digest_index, start=1):
                if not isinstance(item, dict) or not item.get("chunk_id"):
                    continue
                chunk_previews.append(
                    {
                        "ref": f"T{idx:02d}",
                        "chunk_id": item.get("chunk_id", ""),
                        "preview": _compact(item.get("summary"), 260),
                        "title": _compact(item.get("title"), 100),
                        "paper_id": item.get("paper_id", ""),
                        "use_permission": item.get("use_permission", "discovery_only"),
                        "source_kind": item.get("source_kind", "unknown"),
                        "content_depth": item.get("content_depth", "metadata"),
                        "digest_ref": item.get("ref", ""),
                    }
                )
            evidence_batches = [
                {
                    "batch_id": item.get("batch_id"),
                    "chunk_ids": list(item.get("chunk_ids") or []),
                    "paper_ids": list(item.get("paper_ids") or []),
                    "summary": _compact(item.get("summary"), 900),
                }
                for item in (digest.get("batches") or [])
                if isinstance(item, dict)
            ]
        else:
            chunk_previews = [
                {
                    "ref": f"T{idx:02d}",
                    "chunk_id": c.get("chunk_id", ""),
                    "preview": _compact(c.get("text_preview") or c.get("text") or c.get("search_text", ""), 260),
                    "section_path": _compact(c.get("section_path", ""), 80),
                    "use_permission": c.get("use_permission", "discovery_only"),
                    "scope_fit": c.get("scope_fit", "unreviewed"),
                    "source_kind": c.get("source_kind", "unknown"),
                    "content_depth": c.get("content_depth", "metadata"),
                    "retrieval_role": c.get("retrieval_role", ""),
                    "audit": _compact_audit_provenance(c),
                }
                for idx, c in enumerate(raw_text_chunks, start=1)
                if isinstance(c, dict)
            ]
            evidence_batches = []

        # Only expose visual chunks that passed visual_argument_status == "ok"
        raw_visual = section.get("candidate_visual_chunks") or []
        ok_visual = _filter_ok_visual_chunks(raw_visual)
        visual_previews = [
            {
                "ref": f"V{idx:02d}",
                "chunk_id": v.get("chunk_id", ""),
                "visual_argument_type": v.get("visual_argument_type", ""),
                "caption": _compact(v.get("caption", ""), 200),
            }
            for idx, v in enumerate(ok_visual[:10], start=1)
        ]

        raw_seed = section.get("claim_graph_seed") or {}
        seeds = (
            raw_seed.get("central_claim_candidates") or []
            if isinstance(raw_seed, dict)
            else raw_seed if isinstance(raw_seed, list) else []
        )
        seed_texts = [
            _compact(s.get("claim_seed", "") if isinstance(s, dict) else s, 200)
            for s in seeds[:4]
        ]
        seed_contracts = [
            {
                "claim_seed_id": str(seed.get("claim_seed_id") or f"C{idx:02d}"),
                "claim_seed": _compact(seed.get("claim_seed"), 420),
                "relation_roles": [
                    str(value) for value in _as_list(seed.get("relation_roles"))
                    if str(value).strip()
                ][:8],
                "counterevidence_query": _compact(seed.get("counterevidence_query"), 360),
                "boundary_conditions": [
                    _compact(value, 300)
                    for value in _as_list(seed.get("boundary_conditions"))
                    if _compact(value, 300)
                ][:6],
                "axis_assignments": [
                    dict(value) for value in (seed.get("axis_assignments") or [])
                    if isinstance(value, dict)
                ][:6],
                "counterevidence_text_chunk_ids": [
                    str(value) for value in _as_list(seed.get("counterevidence_text_chunk_ids"))
                    if str(value).strip()
                ][:4],
                "boundary_text_chunk_ids": [
                    str(value) for value in _as_list(seed.get("boundary_text_chunk_ids"))
                    if str(value).strip()
                ][:4],
                "background_text_chunk_ids": [
                    str(value) for value in _as_list(seed.get("background_text_chunk_ids"))
                    if str(value).strip()
                ][:4],
            }
            for idx, seed in enumerate(seeds[:4], start=1)
            if isinstance(seed, dict)
        ]
        return {
            "section_id": section.get("section_id", ""),
            "section_title": _compact(section.get("title", ""), 180),
            "argument_role": _compact(section.get("argument_role", ""), 360),
            "section_contract": {
                "core_question": _compact(contract.get("core_question"), 900),
                "central_thesis": _compact(contract.get("central_thesis"), 900),
                "argument_tasks": list(contract.get("argument_tasks") or [])[:12],
                "argument_sequence": list(contract.get("argument_sequence") or [])[:8],
                "paragraph_functions": list(contract.get("paragraph_functions") or [])[:10],
                "key_questions": list(contract.get("key_questions") or [])[:12],
                "scope_guardrails": list(contract.get("scope_guardrails") or [])[:12],
                "transitions": dict(contract.get("transitions") or {}),
                "mentor_guidance": [
                    _compact(item, 700)
                    for item in (contract.get("mentor_guidance") or [])[:8]
                    if _compact(item, 700)
                ],
                "synthesis_task": _compact(contract.get("synthesis_task"), 1000),
                "transition_from_previous": _compact(contract.get("transition_from_previous"), 700),
                "transition_to_next": _compact(contract.get("transition_to_next"), 700),
                "target_word_range": list(contract.get("target_word_range") or [])[:2],
                "visual_argument_slots": [
                    dict(item) for item in (contract.get("visual_argument_slots") or [])[:8]
                    if isinstance(item, dict)
                ],
                "required_evidence_roles": list(
                    contract.get("required_evidence_roles")
                    or section.get("required_claim_kinds")
                    or []
                )[:8],
                "evidence_fallback_policy": dict(
                    contract.get("evidence_fallback_policy") or {}
                ),
                "forbidden_overclaims": list(
                    contract.get("forbidden_overclaims")
                    or section.get("scope_guardrails")
                    or []
                )[:8],
                "open_questions": list(contract.get("open_questions") or [])[:6],
                "word_budget": contract_word_budget,
                "axis_assignments": [
                    dict(item) for item in (contract.get("axis_assignments") or [])
                    if isinstance(item, dict)
                ][:12],
                "argument_structure": dict(contract.get("argument_structure") or {}),
                "candidate_material_pool": dict(contract.get("candidate_material_pool") or {}),
            },
            "review_mentor_advice": section.get("review_mentor_advice", {}),
            "claim_seeds": seed_texts,
            "claim_seed_contracts": seed_contracts,
            "candidate_text_chunks": chunk_previews,
            "evidence_batches": evidence_batches,
            "candidate_text_model_policy": dict(section.get("candidate_text_model_policy") or {}),
            "evidence_digest_policy": (
                "Read batch summaries and proposition snippets first. Use chunk_id to reopen raw text for exact quote verification; do not infer a claim from a title alone."
                if evidence_batches
                else "No material digest was available; use the compact candidate previews and keep unsupported claims open."
            ),
            "candidate_visual_chunks": visual_previews,
        }

    def _verify_and_arbitrate(
        self,
        section: dict[str, Any],
        claims: list[Claim],
        text_chunks: list[dict[str, Any]],
        *,
        repair: Any = None,
    ) -> list[Claim]:
        """Run the authoritative verifier and evidence-type arbiter gates."""
        # A separate B-model verifier judges whether the selected anchors
        # actually entail each claim.  This is the authoritative binding step.
        try:
            from optomind_research.claim_evidence_verifier import ClaimEvidenceVerifier
            verifier = ClaimEvidenceVerifier(
                model_tier="premium_model",
                strict_permissions=True,
            )
            claims = verifier.verify_and_bind(claims, section)
            self.last_audit["evidence_verifier_initial"] = dict(verifier.last_audit)
            seed_audit = audit_claim_seed_coverage(section, claims)
            core_fit = [
                c for c in claims
                if c.section_fit in {"central", "supporting"}
            ]
            boundary_count = sum(1 for c in claims if c.section_fit == "boundary")
            missing_seed_texts = list(seed_audit.get("missing_seed_texts") or [])
            if (
                repair is not None
                and (
                    len(core_fit) < 2
                    or boundary_count > 1
                    or any(c.section_fit == "off_scope" for c in claims)
                    or missing_seed_texts
                )
            ):
                rejected = [
                    c.statement for c in claims
                    if c.section_fit in {"boundary", "off_scope"}
                    or c.evidence_binding_status in {"insufficient", "contradicted"}
                ]
                repaired = repair(
                    build_claim_set_repair_instruction(
                        missing_seed_texts=missing_seed_texts,
                        rejected_statements=rejected,
                        seed_audit_stage="verifier-stage seed coverage audit",
                    )
                )
                repaired = verifier.verify_and_bind(repaired, section)
                self.last_audit["evidence_verifier_repair"] = dict(verifier.last_audit)

                if (
                    rank_claim_set_quality(repaired, section) > rank_claim_set_quality(claims, section)
                    and sum(1 for c in repaired if c.section_fit in {"central", "supporting"}) >= 2
                ):
                    claims = repaired
        except Exception as _exc:
            self.last_audit["evidence_verifier_error"] = type(_exc).__name__
            import logging as _log
            _log.getLogger(__name__).warning("claim evidence verifier failed: %s", _exc)
            # Deterministic fallback is intentionally conservative and replaces
            # unverified model bindings rather than blessing them.
            try:
                from optomind_research.claim_evidence_binder import bind_claims_to_chunks
                claims = bind_claims_to_chunks(
                    claims,
                    text_chunks,
                    top_k=3,
                    replace_existing=True,
                    min_score=0.35,
                )
            except Exception as bind_exc:
                _log.getLogger(__name__).warning("bind_claims_to_chunks failed: %s", bind_exc)
            for c in claims:
                c.evidence_binding_status = c.evidence_binding_status or "unverified"
                c.evidence_binding_confidence = c.evidence_binding_confidence or "low"
                c.critic_flags.append(f"evidence_verifier_error: {type(_exc).__name__}")

        # Evidence role arbitration is a distinct task and must run for every
        # real, successfully parsed claim set.
        try:
            from optomind_research.evidence_arbiter import EvidenceTypeArbiter
            arbiter = EvidenceTypeArbiter(model_tier="advanced_model")
            claims = arbiter.arbitrate_section(claims, section)
            self.last_audit["arbiter"] = dict(getattr(arbiter, "last_audit", {}) or {})
            self.last_audit["arbiter_call_count"] = int(
                self.last_audit["arbiter"].get("call_count") or 0
            )
        except Exception as exc:
            self.last_audit["arbiter"] = {
                "call_count": 0,
                "attempts": [{
                    "model": "advanced_model",
                    "error": type(exc).__name__,
                    "usage": {},
                    "usage_recorded": False,
                    "retries": 1,
                    "max_retries": 1,
                    "failed": True,
                }],
                "failed": True,
            }
            self.last_audit["arbiter_call_count"] = 0
            for c in claims:
                c.evidence_type_confidence = "not_run"
                c.critic_flags.append(f"arbiter_error: {type(exc).__name__}")
        return claims

    def _llm_decompose(self, section: dict[str, Any]) -> list[Claim]:
        self.last_audit = {"generation_attempts": []}
        section_id = section.get("section_id", "S00")
        text_chunks = section.get("candidate_text_chunks") or []
        text_ids_from_chunks = [str(c.get("chunk_id") or "") for c in text_chunks if isinstance(c, dict) and c.get("chunk_id")]
        valid_text_ids = set(section.get("candidate_text_chunk_ids") or []) | set(text_ids_from_chunks)
        text_ref_map = {
            f"T{idx:02d}": str(c.get("chunk_id"))
            for idx, c in enumerate(text_chunks, start=1)
            if isinstance(c, dict) and c.get("chunk_id")
        }
        ok_visual = _filter_ok_visual_chunks(
            section.get("candidate_visual_chunks") or []
        )
        valid_visual_ids = {v.get("chunk_id", "") for v in ok_visual if v.get("chunk_id")}
        visual_ref_map = {
            f"V{idx:02d}": str(v.get("chunk_id"))
            for idx, v in enumerate(ok_visual[:10], start=1)
            if v.get("chunk_id")
        }
        payload = self._build_input_payload(section)
        self.last_input_payload = payload

        if self._claim_pool_active(section):
            pool = self.build_candidate_claim_pool(section)
            section["candidate_claim_pool"] = pool
            section["candidate_claim_pool_audit"] = pool.get("audit", {})
            _is_aborted = pool.get("pool_status") == "aborted" or bool(
                (pool.get("audit") or {}).get("section_aborted")
            )
            if _is_aborted:
                _partial = pool.get("claims") or []
                _pool_audit = pool.get("audit") or {}
                _batches_done = int(
                    _pool_audit.get("batches_completed_before_abort") or 0
                )
                _abort_reason = str(_pool_audit.get("abort_reason") or "")
                _abort_error = str(_pool_audit.get("abort_error") or "")
                # The candidate-pool audit sees the planned batch list, so it
                # can identify a failed batch even though the public ``batches``
                # list intentionally contains only successfully committed rows.
                _failed_batch_id = str(
                    _pool_audit.get("failed_batch_id") or ""
                )
                if not _partial:
                    # Truly no claims recovered — record and abort
                    section["candidate_claim_pool_shortlist_audit"] = {
                        "pool_claim_count": 0,
                        "selected_count": 0,
                        "section_aborted": True,
                        "partial_recovery": False,
                        "completed_batch_count": _batches_done,
                        "failed_batch_id": _failed_batch_id,
                        "abort_reason": _abort_reason,
                        "abort_error": _abort_error,
                        "policy": (
                            "bounded transport failure: no batches completed"
                            " before abort"
                        ),
                    }
                    self.last_audit["claim_pool_section_aborted"] = True
                    self.last_audit["legacy_single_call_used"] = False
                    _update_claim_pool_checkpoint_audit(
                        Path(self.last_audit.get("claim_pool_checkpoint_path"))
                        if self.last_audit.get("claim_pool_checkpoint_path")
                        else None,
                        {"selected_count": 0},
                    )
                    return []
                # Partial recovery: earlier batches succeeded, later ones failed
                import logging as _log
                _log.getLogger(__name__).warning(
                    "claim_pool aborted for section %s but %d claims recovered"
                    " from %d completed batch(es); continuing with partial pool.",
                    section.get("section_id", "?"),
                    len(_partial),
                    _batches_done,
                )
                section["claim_pool_partial_recovery"] = True
                self.last_audit["claim_pool_section_aborted"] = True
                self.last_audit["claim_pool_partial_recovery"] = True
                self.last_audit["claim_pool_partial_recovery_count"] = len(_partial)
                self.last_audit["claim_pool_completed_batch_count"] = _batches_done
                self.last_audit["claim_pool_failed_batch_id"] = _failed_batch_id
                self.last_audit["legacy_single_call_used"] = False
                # fall through to _select_final_claims_from_pool with partial pool
            claims = self._select_final_claims_from_pool(pool, section)
            section["candidate_claim_pool_shortlist_audit"] = {
                "pool_claim_count": len(pool.get("claims") or []),
                "selected_count": len(claims),
                "section_aborted": _is_aborted,
                "partial_recovery": bool(
                    self.last_audit.get("claim_pool_partial_recovery")
                ),
                "completed_batch_count": int(
                    self.last_audit.get("claim_pool_completed_batch_count") or 0
                ),
                "failed_batch_id": str(
                    self.last_audit.get("claim_pool_failed_batch_id") or ""
                ),
                "abort_reason": str(
                    (pool.get("audit") or {}).get("abort_reason") or ""
                ),
                "abort_error": str(
                    (pool.get("audit") or {}).get("abort_error") or ""
                ),
                "selection_limit": self.final_claim_selection_limit,
                "policy": (
                    "downstream shortlist only; the stored candidate claim "
                    "pool remains complete and audited"
                ),
                "diversity": dict(
                    self.last_audit.get(
                        "candidate_claim_shortlist_diversity"
                    )
                    or {}
                ),
            }
            self.last_audit["claim_pool_claims_selected"] = len(claims)
            _update_claim_pool_checkpoint_audit(
                Path(self.last_audit.get("claim_pool_checkpoint_path"))
                if self.last_audit.get("claim_pool_checkpoint_path")
                else None,
                {"selected_count": len(claims)},
            )
            self.last_audit["legacy_single_call_used"] = False
            if not self.verify_candidate_pool_claims:
                self.last_audit["formal_verification_deferred"] = True
                self.last_audit["formal_verification_policy"] = (
                    "deferred_to_later_explicit_stage"
                )
                section.setdefault(
                    "candidate_claim_pool_shortlist_audit", {}
                )["formal_verification_deferred"] = True
                section["candidate_claim_pool_shortlist_audit"][
                    "formal_verification_policy"
                ] = "deferred_to_later_explicit_stage"
                for claim in claims:
                    claim.critic_flags.append("formal_verification_deferred")
                    claim.evidence_binding_status = "unverified"
                return claims
            return self._verify_and_arbitrate(
                section, claims, text_chunks, repair=None
            )
        self.last_audit["legacy_single_call_used"] = True

        def _generate(extra_instruction: str = "") -> list[Claim]:
            call_payload = dict(payload)
            if extra_instruction:
                call_payload["repair_instruction"] = extra_instruction
            estimated_input_tokens = max(
                1, len(json.dumps(call_payload, ensure_ascii=False)) // 4
            )
            try:
                result = call_qwen_chat(
                    "ClaimDecomposerAgent",
                    [
                        {"role": "system", "content": self._load_prompt()},
                        {"role": "user", "content": json.dumps(call_payload, ensure_ascii=False)},
                    ],
                    model_tier=self.model_tier,
                    temperature=0,
                    max_tokens=4200,
                    response_format={"type": "json_object"},
                    stream=True,
                    force_mock=False,
                    max_retries=1,
                )
            except Exception as exc:
                self.last_audit["generation_attempts"].append({
                    "model": self.model_tier,
                    "model_tier": self.model_tier,
                    "max_tokens": 4200,
                    "estimated_input_tokens": estimated_input_tokens,
                    "estimated_output_tokens": 0,
                    "raw_chars": 0,
                    "usage": {},
                    "usage_recorded": False,
                    "retries": 1,
                    "max_retries": 1,
                    "failed": True,
                    "error": type(exc).__name__,
                })
                raise
            usage = result.get("_llm_usage")
            if not isinstance(usage, dict):
                usage = {}
            self.last_audit["generation_attempts"].append(
                {
                    "model": self.model_tier,
                    "model_tier": self.model_tier,
                    "max_tokens": 4200,
                    "estimated_input_tokens": estimated_input_tokens,
                    "estimated_output_tokens": max(1, len(str(result.get("content") or "")) // 4),
                    "raw_chars": len(str(result.get("content") or "")),
                    "usage": dict(usage),
                    "usage_recorded": bool(usage),
                    "retries": int(usage.get("retry_count") or 0),
                    "max_retries": 1,
                    "failed": bool(result.get("error") or result.get("failure")),
                }
            )
            parsed = _safe_json(str(result.get("content") or ""))
            return _parse_llm_claims(
                parsed,
                section_id,
                valid_text_ids,
                text_ref_map,
                valid_visual_ids,
                visual_ref_map,
            )

        claims = _generate()
        if len(claims) < 2:
            claims = _generate(
                "The prior response did not yield at least 2 valid claims. Return 2-8 non-filler claims and use only the supplied T/V references."
            )
        if 2 <= len(claims) <= 8:
            pre_verify_seed_audit = audit_claim_seed_coverage(section, claims)
            pre_verify_missing_seed_texts = list(pre_verify_seed_audit.get("missing_seed_texts") or [])
            if pre_verify_missing_seed_texts:
                claims = _generate(
                    build_claim_set_repair_instruction(
                        missing_seed_texts=pre_verify_missing_seed_texts,
                        seed_audit_stage="deterministic pre-verifier seed coverage audit",
                    )
                )
        return self._verify_and_arbitrate(
            section, claims, text_chunks, repair=_generate
        )


def _build_evidence_network(sections: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute EvidenceNetwork summary from sections with claims already set."""
    chunk_to_claims: dict[str, list[str]] = {}
    nodes: dict[str, dict] = {}
    for section in sections:
        for claim in section.get("claims") or []:
            cid = claim.get("claim_id", "")
            nodes[cid] = claim
            for chunk_id in claim.get("supporting_text_chunk_ids") or []:
                chunk_to_claims.setdefault(chunk_id, []).append(cid)
            for chunk_id in claim.get("supporting_visual_chunk_ids") or []:
                chunk_to_claims.setdefault(chunk_id, []).append(cid)

    load_bearing_chunks = sorted(
        chunk_id for chunk_id, claimers in chunk_to_claims.items() if len(claimers) >= 2
    )
    load_bearing_claims = [
        cid for cid, c in nodes.items() if c.get("load_bearing")
    ]
    return {
        "total_claims": len(nodes),
        "total_chunks_referenced": len(chunk_to_claims),
        "load_bearing_chunks": load_bearing_chunks,
        "load_bearing_claims": load_bearing_claims,
    }


def decompose_blueprint(
    blueprint: dict[str, Any],
    *,
    real_llm: bool = False,
    model_tier: str = "standard_model",
    prompt_path: Path = DEFAULT_DECOMPOSER_PROMPT,
    claim_pool_enabled: bool | None = None,
) -> dict[str, Any]:
    """Add 'claims' list to every section in blueprint. Returns modified blueprint."""
    decomposer = ClaimDecomposer(
        prompt_path=prompt_path,
        model_tier=model_tier,
        real_llm=real_llm,
        claim_pool_enabled=claim_pool_enabled,
    )
    sections = blueprint.get("sections") or []
    for section in sections:
        claims = decomposer.decompose_section(section)
        section["claims"] = [c.to_dict() for c in claims]
    blueprint["evidence_network"] = _build_evidence_network(sections)
    blueprint["claim_decomposition_status"] = {
        "sections_processed": len(sections),
        "real_llm": real_llm,
        "model_tier": model_tier,
        "candidate_claim_pool_enabled": claim_pool_enabled,
    }
    return blueprint
