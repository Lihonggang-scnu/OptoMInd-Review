"""Source-grounded verification and binding for M2a review claims.

The decomposer is a generative component.  This verifier is a separate,
batched judging step that maps short anchor references back to opaque chunk
IDs and records whether the cited text actually supports each claim.
"""

from __future__ import annotations

import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from llm.qwen_chat_client import call_qwen_chat
from optomind_research.claim_schema import Claim
from optomind_research.runtime.argument_quality_policy import (
    BACKGROUND,
    DISCOVERY,
    FACTUAL,
    QUALIFIED,
    evidence_ceiling,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT = PROJECT_ROOT / "prompts" / "Claim Evidence Verifier.txt"
_VALID_VERDICTS = {"direct", "synthesized", "partial", "insufficient", "contradicted"}
_VALID_CONFIDENCE = {"high", "medium", "low"}
_VALID_SECTION_FIT = {"central", "supporting", "boundary", "off_scope"}


def _compact(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _safe_json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", str(text or ""), re.S)
        if not match:
            return {}
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}


def build_anchor_maps(section: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return stable short-reference maps for the candidate anchors."""
    text_map: dict[str, dict[str, Any]] = {}
    # The caller has already supplied the bounded, portfolio-selected M2a
    # view.  Do not apply a second arbitrary first-16 cut here.
    for idx, row in enumerate((section.get("candidate_text_chunks") or []), start=1):
        if not isinstance(row, dict) or not row.get("chunk_id"):
            continue
        text_map[f"T{idx:02d}"] = row
    visual_map: dict[str, dict[str, Any]] = {}
    for idx, row in enumerate((section.get("candidate_visual_chunks") or [])[:10], start=1):
        if not isinstance(row, dict) or not row.get("chunk_id"):
            continue
        if row.get("visual_argument_status") != "ok":
            continue
        visual_map[f"V{idx:02d}"] = row
    return text_map, visual_map


def _paper_identity(row: dict[str, Any]) -> str:
    value = str(row.get("paper_id") or row.get("doi") or "").strip().lower()
    if value:
        return value
    chunk_id = str(row.get("chunk_id") or "")
    return chunk_id.split(":", 1)[0].lower()


def _evidence_kind(row: dict[str, Any]) -> str:
    value = str(row.get("source_kind") or row.get("evidence_level") or "").strip().lower()
    if value:
        return value
    raw = row.get("raw_json")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            value = str(parsed.get("source_kind") or parsed.get("evidence_level") or "").strip().lower()
            if value:
                return value
        except Exception:
            pass
    return "fulltext"


def _compact_audit_metadata(row: dict[str, Any]) -> dict[str, Any]:
    """Return the small provenance envelope safe to send to an LLM.

    The canonical graph keeps the complete route/provenance object locally.
    The verifier only needs enough information to apply the permission
    ceiling and distinguish discovery from materialization.  In particular,
    never pass the nested legacy provenance object through unchanged.
    """
    raw = row.get("provenance")
    raw = raw if isinstance(raw, dict) else {}

    def pick(*keys: str, limit: int = 180) -> str:
        for key in keys:
            value = row.get(key)
            if value in (None, ""):
                value = raw.get(key)
            if value not in (None, ""):
                return _compact(value, limit)
        return ""

    source_locator = row.get("source_locator")
    if not isinstance(source_locator, dict):
        source_locator = raw.get("source_locator")
    if not isinstance(source_locator, dict):
        source_locator = {}
    # Keep only scalar locator fields.  The full route object remains local.
    compact_locator = {
        str(key): value
        for key, value in source_locator.items()
        if isinstance(value, (str, int, float, bool)) and value not in ("", None)
    }
    metadata = {
        "provider": pick("provider", "source_provider"),
        "doi": pick("doi", "paper_doi"),
        "corpus_id": pick("corpus_id", "CorpusId", "s2_corpus_id"),
        "locator": pick("locator", "section_path", "source_url", "url", limit=220),
        "discovery_route": pick("discovery_route", limit=180),
        "materialization_route": pick("materialization_route", limit=180),
        "use_permission": pick("use_permission", limit=80) or DISCOVERY,
        "scope_fit": pick("scope_fit", limit=80) or "unreviewed",
        "content_depth": pick("content_depth", limit=80) or "metadata",
        "source_kind": pick("source_kind", "evidence_level", limit=80) or _evidence_kind(row),
        "context_complete": bool(row.get("context_complete", raw.get("context_complete", False))),
        "source_locator": compact_locator,
    }
    return {
        key: value for key, value in metadata.items()
        if value not in ("", None) and value != {}
    }


def _anchor_terms(text: str) -> set[str]:
    stop = {
        "the", "and", "for", "with", "from", "that", "this", "into", "using",
        "claim", "section", "paper", "study", "show", "shows", "based", "under",
    }
    return {
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", str(text or ""))
        if token.casefold() not in stop
    }


def _numeric_literals(text: str) -> set[str]:
    """Return comparable numeric literals, excluding ordinary publication years.

    This is deliberately a boundary check, not a strict numerical audit.  It
    catches attractive precision introduced by a model (for example, a power
    value absent from every selected anchor) while allowing formatting variants
    such as ``0.90`` versus ``0.9``.
    """
    values: set[str] = set()
    for raw in re.findall(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?(?![A-Za-z0-9.])", str(text or "")):
        try:
            number = Decimal(raw)
        except InvalidOperation:
            continue
        if number == number.to_integral() and 1900 <= int(number) <= 2100:
            continue
        normalized = format(number.normalize(), "f")
        values.add(normalized.rstrip("0").rstrip(".") if "." in normalized else normalized)
    return values


def _normalized_quote(value: Any) -> str:
    """Normalize only whitespace/unicode for deterministic quote matching."""
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", str(value or "")),
    ).strip()


def _verify_quote(row: dict[str, Any], quote: Any) -> tuple[bool, int, int, str]:
    """Check a model quote against the complete local chunk when available.

    The verifier prompt asks for verbatim spans, but prompt compliance alone
    is not a provenance guarantee.  This deterministic check prevents a model
    from turning a paraphrase into an exact citation.  Offsets are relative to
    the normalized chunk text; the source locator carries the original
    document offsets when the ingestion route supplied them.
    """
    normalized_quote = _normalized_quote(quote)
    if not normalized_quote:
        return False, -1, -1, "empty_quote"
    source = row.get("normalized_text") or row.get("text") or ""
    # Pre-R3 callers supplied only a bounded preview.  Preserve that
    # compatibility path, but label it clearly: it is not a deterministic
    # full-document quote check.  Canonical Phase-3 graph records always carry
    # normalized_text, so real production bindings remain fail-closed.
    if not source:
        if row.get("text_preview"):
            return True, -1, -1, "preview_only_legacy"
        return False, -1, -1, "no_source_text"
    normalized_source = _normalized_quote(source)
    if not normalized_source:
        return False, -1, -1, "no_source_text"
    start = normalized_source.find(normalized_quote)
    if start < 0:
        return False, -1, -1, "quote_not_found"
    return True, start, start + len(normalized_quote), "normalized_exact"


class ClaimEvidenceVerifier:
    def __init__(
        self,
        *,
        prompt_path: Path = DEFAULT_PROMPT,
        model_tier: str = "advanced_model",
        strict_permissions: bool = False,
    ) -> None:
        self.prompt_path = Path(prompt_path)
        self.model_tier = model_tier
        # Old standalone callers supplied only text previews.  Keep that
        # compatibility path auditable, while Phase 3 passes True so missing
        # permission metadata is fail-closed in the real orchestration chain.
        self.strict_permissions = bool(strict_permissions)
        self.last_audit: dict[str, Any] = {}

    def verify_and_bind(self, claims: list[Claim], section: dict[str, Any]) -> list[Claim]:
        if not claims:
            return claims
        text_map, visual_map = build_anchor_maps(section)
        if not text_map:
            for claim in claims:
                claim.supporting_text_chunk_ids = []
                claim.saturation_score = 0.0
                claim.evidence_binding_status = "insufficient"
                claim.evidence_binding_confidence = "high"
                claim.evidence_binding_reason = "No candidate text anchors were available."
                claim.critic_flags.append("no_candidate_text_anchors")
            return claims

        if not self.strict_permissions:
            # Compatibility for pre-R3 callers whose hand-written fixtures
            # predate the canonical permission protocol.  Explicit
            # discovery/background/transfer values are never changed.  Real
            # Phase 3 calls use strict_permissions=True and therefore cannot
            # take this defaulting path.
            for row in text_map.values():
                if str(row.get("use_permission") or "").strip():
                    continue
                source_kind = str(
                    row.get("source_kind") or row.get("evidence_level") or ""
                ).strip().casefold()
                row["legacy_permission_defaulted"] = True
                if source_kind in {"abstract", "tldr", "metadata", "title", "snippet"}:
                    row["use_permission"] = QUALIFIED
                else:
                    row["use_permission"] = FACTUAL
                    row.setdefault("scope_fit", "direct")
                    row.setdefault("content_depth", "fulltext")
                    row.setdefault("context_complete", True)

        # Build compact anchor records once.  ``text_map`` remains complete
        # for local ID/permission validation, but only a small per-batch
        # subset is sent to the model.
        compact_text_by_ref: dict[str, dict[str, Any]] = {}
        chunk_to_ref: dict[str, str] = {}
        for ref, row in text_map.items():
            preview = row.get("text_preview") or row.get("text") or row.get("search_text") or ""
            compact_text_by_ref[ref] = {
                "ref": ref,
                "paper_id": _compact(row.get("paper_id") or row.get("source_paper_id") or row.get("doi"), 120),
                "doi": _compact(row.get("doi"), 120),
                "title": _compact(row.get("title"), 160),
                "preview": _compact(preview, 700),
                "audit": _compact_audit_metadata(row),
                "retrieval_role": _compact(row.get("retrieval_role"), 80),
                "provisional": _evidence_kind(row) == "abstract",
            }
            chunk_id = str(row.get("chunk_id") or "").strip()
            if chunk_id:
                chunk_to_ref[chunk_id] = ref
        compact_visual_by_ref: dict[str, dict[str, Any]] = {
            ref: {
                "ref": ref,
                "visual_argument_type": _compact(row.get("visual_argument_type"), 80),
                "caption": _compact(row.get("caption") or row.get("search_text"), 350),
            }
            for ref, row in visual_map.items()
        }
        compact_section = {
            "section_id": _compact(section.get("section_id"), 80),
            "title": _compact(section.get("title"), 180),
            "argument_role": _compact(section.get("argument_role"), 350),
        }
        prompt = self.prompt_path.read_text(encoding="utf-8")
        expected_claim_ids = {c.claim_id for c in claims}
        claim_payload_by_id = {
            str(c.claim_id): {
                "claim_id": c.claim_id,
                "statement": _compact(c.statement, 900),
                "evidence_type": c.evidence_type,
            }
            for c in claims
        }

        def _batch_anchor_refs(batch_ids: list[str]) -> tuple[list[str], list[str]]:
            """Return referenced anchors plus at most two alternatives."""
            batch_claims = [claim for claim in claims if claim.claim_id in batch_ids]
            referenced: list[str] = []
            visual_referenced: list[str] = []
            query_terms: set[str] = set()
            for claim in batch_claims:
                query_terms.update(_anchor_terms(claim.statement))
                raw_ids = list(getattr(claim, "supporting_text_chunk_ids", []) or []) + list(
                    getattr(claim, "context_text_chunk_ids", []) or []
                )
                for chunk_id in raw_ids:
                    ref = chunk_to_ref.get(str(chunk_id))
                    if ref and ref not in referenced:
                        referenced.append(ref)
                for visual_id in list(claim.supporting_visual_chunk_ids or []):
                    ref = next(
                        (key for key, value in visual_map.items()
                         if str(value.get("chunk_id") or "") == str(visual_id)),
                        "",
                    )
                    if ref and ref not in visual_referenced:
                        visual_referenced.append(ref)
            scored: list[tuple[int, str]] = []
            for ref, row in text_map.items():
                if ref in referenced:
                    continue
                preview = str(row.get("text_preview") or row.get("text") or row.get("search_text") or "")
                score = len(query_terms & _anchor_terms(preview))
                if score:
                    scored.append((score, ref))
            scored.sort(key=lambda item: (-item[0], item[1]))
            referenced.extend(ref for _, ref in scored[:2])
            return referenced, visual_referenced

        def verify_batch(batch_ids: list[str], batch_index: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            """Verify a small batch, then retry only missing IDs.

            The old all-claims call routinely truncated a four-claim response:
            one complete binding is often about one thousand output tokens.
            Small parallel batches reduce latency and make partial failure
            attributable without weakening the fail-closed policy.
            """
            batch_rows: list[dict[str, Any]] = []
            batch_attempts: list[dict[str, Any]] = []
            remaining = list(batch_ids)
            for attempt_index in range(2):
                if not remaining:
                    break
                text_refs, visual_refs = _batch_anchor_refs(remaining)
                call_payload = {
                    "section": compact_section,
                    "claims": [claim_payload_by_id[cid] for cid in remaining],
                    "text_anchors": [compact_text_by_ref[ref] for ref in text_refs],
                    "visual_anchors": [compact_visual_by_ref[ref] for ref in visual_refs],
                }
                estimated_payload_tokens = max(
                    1, len(json.dumps(call_payload, ensure_ascii=False)) // 4
                )
                repair_note = ""
                if attempt_index:
                    repair_note = (
                        "\nThe previous answer omitted these claim IDs: "
                        + ", ".join(remaining)
                        + ". Return one complete binding for every listed claim_id. "
                        "Do not repeat already completed claims and do not add prose outside JSON."
                    )
                # A missing/invalid answer gets one bounded second opinion.  Do
                # not let a single evidence item rotate through every key and
                # configured fallback model: verification is fail-closed and a
                # later revision round can retry the unresolved item.
                tier = self.model_tier if attempt_index == 0 else (
                    "advanced_model" if self.model_tier == "premium_model" else "premium_model"
                )
                # Budget for the verbose evidence-span schema rather than the
                # former fixed 2400-token ceiling that silently truncated output.
                max_tokens = min(6200, max(2600, 1500 * len(remaining) + 900))
                try:
                    result = call_qwen_chat(
                        "ClaimEvidenceVerifierAgent",
                        [
                            {"role": "system", "content": prompt + repair_note},
                            {"role": "user", "content": json.dumps(call_payload, ensure_ascii=False)},
                        ],
                        model_tier=tier,
                        temperature=0,
                        max_tokens=max_tokens,
                        response_format={"type": "json_object"},
                        stream=True,
                        force_mock=False,
                        max_retries=0,
                        timeout_seconds=75,
                        max_transport_key_candidates=1,
                        allow_model_fallback=False,
                    )
                except Exception as exc:
                    batch_attempts.append({
                        "batch": batch_index,
                        "attempt": attempt_index + 1,
                        "requested_claim_ids": list(remaining),
                        "model": tier,
                        "model_tier": tier,
                        "max_tokens": max_tokens,
                        "anchor_refs_sent": len(text_refs),
                        "estimated_input_tokens": estimated_payload_tokens,
                        "returned_claim_ids": [],
                        "raw_chars": 0,
                        "usage": {},
                        "usage_recorded": False,
                        "retries": attempt_index + 1,
                        "max_retries": 1,
                        "failed": True,
                        "error": type(exc).__name__,
                    })
                    continue
                raw = str(result.get("content") or "")
                parsed = _safe_json(raw)
                candidate_rows = (
                    parsed.get("bindings") if isinstance(parsed.get("bindings"), list) else []
                )
                candidate_rows = [row for row in candidate_rows if isinstance(row, dict)]
                returned_ids = {
                    str(row.get("claim_id") or "") for row in candidate_rows
                    if str(row.get("claim_id") or "") in batch_ids
                }
                batch_rows.extend(
                    row for row in candidate_rows
                    if str(row.get("claim_id") or "") in returned_ids
                )
                usage = result.get("_llm_usage")
                if not isinstance(usage, dict):
                    usage = {}
                batch_attempts.append({
                    "batch": batch_index,
                    "attempt": attempt_index + 1,
                    "requested_claim_ids": list(remaining),
                    "model": tier,
                    "model_tier": tier,
                    "max_tokens": max_tokens,
                    "anchor_refs_sent": len(text_refs),
                    "estimated_input_tokens": estimated_payload_tokens,
                    "returned_claim_ids": sorted(returned_ids),
                    "raw_chars": len(raw),
                    "usage": dict(usage),
                    "usage_recorded": bool(usage),
                    "retries": attempt_index + int(usage.get("retry_count") or 0),
                    "max_retries": 1,
                    "failed": bool(result.get("error") or result.get("failure")),
                })
                completed = {
                    str(row.get("claim_id") or "") for row in batch_rows
                }
                remaining = [cid for cid in batch_ids if cid not in completed]
            # Last valid answer for a claim wins if a retry duplicated it.
            by_id = {
                str(row.get("claim_id") or ""): row for row in batch_rows
                if row.get("claim_id")
            }
            return list(by_id.values()), batch_attempts

        claim_ids = [claim.claim_id for claim in claims]
        batches = [claim_ids[index:index + 2] for index in range(0, len(claim_ids), 2)]
        if len(batches) > 1:
            with ThreadPoolExecutor(max_workers=min(3, len(batches))) as pool:
                batch_results = list(
                    pool.map(
                        lambda pair: verify_batch(pair[1], pair[0]),
                        enumerate(batches, start=1),
                    )
                )
        else:
            batch_results = [verify_batch(batches[0], 1)] if batches else []
        rows = [row for batch_rows, _ in batch_results for row in batch_rows]
        attempts = [row for _, batch_attempts in batch_results for row in batch_attempts]
        self.last_audit = {
            "expected_claim_ids": sorted(expected_claim_ids),
            "returned_claim_ids": sorted({str(row.get("claim_id") or "") for row in rows}),
            "complete": expected_claim_ids <= {str(row.get("claim_id") or "") for row in rows},
            "batch_size": 2,
            "batch_count": len(batches),
            "attempts": attempts,
            "abstract_anchor_count": sum(1 for row in text_map.values() if _evidence_kind(row) == "abstract"),
        }
        by_claim = {
            str(row.get("claim_id")): row
            for row in rows
            if isinstance(row, dict) and row.get("claim_id")
        }
        permission_audits: list[dict[str, Any]] = []

        for claim in claims:
            claim.critic_flags = [
                flag for flag in claim.critic_flags
                if not str(flag).startswith("evidence_binding_")
                and not str(flag).startswith("unverified_quantitative_literals:")
                and str(flag) not in {
                    "evidence_verifier_missing_claim",
                    "synthesized_evidence_requirements_not_met",
                    "evidence_abstract_only",
                    "claim_off_scope_for_section",
                }
            ]
            row = by_claim.get(claim.claim_id)
            if not row:
                claim.evidence_binding_status = "unverified"
                claim.evidence_binding_confidence = "low"
                claim.evidence_binding_reason = "Verifier returned no binding for this claim."
                claim.saturation_score = min(float(claim.saturation_score), 1.0)
                claim.critic_flags.append("evidence_verifier_missing_claim")
                continue

            verdict = str(row.get("verdict") or "insufficient").lower().strip()
            if verdict not in _VALID_VERDICTS:
                verdict = "insufficient"
            confidence = str(row.get("confidence") or "low").lower().strip()
            if confidence not in _VALID_CONFIDENCE:
                confidence = "low"
            section_fit = str(row.get("section_fit") or "boundary").lower().strip()
            if section_fit not in _VALID_SECTION_FIT:
                section_fit = "boundary"
            claim.section_fit = section_fit
            claim.section_fit_reason = _compact(row.get("section_fit_reason"), 500)
            if section_fit == "off_scope":
                claim.critic_flags.append("claim_off_scope_for_section")
            # Validate quoted spans against the complete local chunk before
            # allowing them to influence the verdict.  The model may still
            # identify a candidate reference, but an unverified paraphrase is
            # not an exact source binding.
            validated_span_rows: list[tuple[dict[str, Any], str, bool, int, int, str]] = []
            invalid_span_refs: list[str] = []
            for span in (row.get("evidence_spans") or [])[:6]:
                if not isinstance(span, dict):
                    continue
                ref = str(span.get("text_ref") or "")
                if ref not in text_map:
                    continue
                verified, start, end, match_mode = _verify_quote(
                    text_map[ref], span.get("quote")
                )
                if not verified:
                    invalid_span_refs.append(ref)
                validated_span_rows.append((span, ref, verified, start, end, match_mode))
            span_policy_by_ref = {
                ref: (
                    str(span.get("scope_fit") or "in_domain").strip().lower(),
                    str(span.get("retrieval_role") or "evidence_candidate").strip().lower(),
                )
                for span, ref, verified, _start, _end, _mode in validated_span_rows
                if verified
            }
            verified_span_by_ref = {
                ref: (span, start, end, match_mode)
                for span, ref, verified, start, end, match_mode in validated_span_rows
                if verified
            }
            raw_refs = [
                str(ref) for ref in (row.get("supporting_text_refs") or [])
                if str(ref) in text_map
            ]
            raw_refs.extend(
                str(ref)
                for component in (row.get("supported_components") or [])[:8]
                if isinstance(component, dict)
                for ref in (component.get("text_refs") or [])[:6]
                if str(ref) in text_map
            )
            raw_refs.extend(
                ref
                for _span, ref, verified, _start, _end, _mode in validated_span_rows
                if verified and ref in text_map
            )
            raw_refs = list(dict.fromkeys(raw_refs))[:10]

            def ref_ceiling(ref: str) -> tuple[str, str]:
                raw_ceiling, reason = evidence_ceiling(text_map[ref])
                model_scope, model_role = span_policy_by_ref.get(
                    ref, ("in_domain", "evidence_candidate")
                )
                if model_scope in {"off_domain", "off_scope"} or model_role == "reject":
                    return DISCOVERY, "model_scope_rejects_anchor"
                if model_scope != "in_domain" or model_role != "evidence_candidate":
                    if raw_ceiling in {FACTUAL, QUALIFIED}:
                        return QUALIFIED, "model_qualified_scope_cannot_be_direct"
                    return raw_ceiling, reason
                return raw_ceiling, reason

            permission_by_ref = {ref: ref_ceiling(ref) for ref in raw_refs}
            rejected_refs = [
                ref for ref, (ceiling, _) in permission_by_ref.items()
                if ceiling in {DISCOVERY, BACKGROUND}
            ]
            eligible_refs = [
                ref for ref in raw_refs
                if permission_by_ref[ref][0] in {FACTUAL, QUALIFIED}
            ][:8]
            factual_refs = [
                ref for ref in eligible_refs if permission_by_ref[ref][0] == FACTUAL
            ]
            qualified_refs = [
                ref for ref in eligible_refs if permission_by_ref[ref][0] == QUALIFIED
            ]
            # Prefer direct in-scope anchors when a claim has them.  A
            # transfer/analogy anchor remains auditable and can support a
            # qualified-only claim, but it must not crowd out the factual
            # anchors in the primary support list.
            text_refs = factual_refs or qualified_refs
            if rejected_refs:
                claim.critic_flags.append("permission_rejected_refs:" + ",".join(rejected_refs))
            if invalid_span_refs:
                claim.critic_flags.append(
                    "unverified_evidence_spans:" + ",".join(dict.fromkeys(invalid_span_refs))
                )
            if qualified_refs and not factual_refs:
                claim.critic_flags.append("permission_ceiling_qualified_only")
            for ref in rejected_refs:
                permission_audits.append({
                    "claim_id": claim.claim_id,
                    "text_ref": ref,
                    "chunk_id": text_map[ref].get("chunk_id", ""),
                    "raw_permission": text_map[ref].get("use_permission", DISCOVERY),
                    "ceiling": permission_by_ref[ref][0],
                    "reason": permission_by_ref[ref][1],
                    "model_requested": True,
                })
            visual_refs = [
                str(ref) for ref in (row.get("supporting_visual_refs") or [])
                if str(ref) in visual_map
            ][:3]

            component_map: list[dict[str, Any]] = []
            for component in (row.get("supported_components") or [])[:8]:
                if not isinstance(component, dict):
                    continue
                component_refs = [
                    str(ref) for ref in (component.get("text_refs") or [])
                    if str(ref) in text_refs
                ][:6]
                if not component_refs:
                    continue
                component_map.append({
                    "component": _compact(component.get("component"), 400),
                    "chunk_ids": [str(text_map[ref]["chunk_id"]) for ref in component_refs],
                })
            # The verifier schema exposes the same support through three
            # complementary fields.  Models occasionally put a valid anchor
            # in ``evidence_spans`` or ``supported_components`` while omitting
            # it from ``supporting_text_refs``.  Treating only the latter as
            # canonical silently discarded already-verified evidence before
            # writing.  Normalize the three views into one bounded support set
            # while still accepting only references from the supplied map.
            component_text_refs = [
                str(ref)
                for component in (row.get("supported_components") or [])[:8]
                if isinstance(component, dict)
                for ref in (component.get("text_refs") or [])[:6]
                if str(ref) in text_refs
            ]
            span_text_refs = [
                ref
                for _span, ref, verified, _start, _end, _mode in validated_span_rows
                if verified and ref in text_refs
            ]
            # Exact spans are the factual writing gate.  A model-selected
            # reference without a verified quote remains an auditable lead,
            # but cannot be emitted as supporting evidence.
            text_refs = list(dict.fromkeys(text_refs + component_text_refs + span_text_refs))[:8]
            if text_refs:
                exact_refs = set(span_text_refs)
                if exact_refs:
                    text_refs = [ref for ref in text_refs if ref in exact_refs]
                else:
                    text_refs = []
                    claim.critic_flags.append("evidence_binding_no_verified_span")
            abstract_refs = [
                ref for ref in text_refs if _evidence_kind(text_map[ref]) == "abstract"
            ]
            abstract_only = bool(text_refs) and len(abstract_refs) == len(text_refs)
            qualified_only = bool(text_refs) and not factual_refs
            missing_components = [
                _compact(value, 300) for value in (row.get("missing_components") or [])[:6]
                if _compact(value, 300)
            ]
            selected_anchor_text = " ".join(
                str(
                    text_map[ref].get("text_preview")
                    or text_map[ref].get("text")
                    or text_map[ref].get("search_text")
                    or ""
                )
                for ref in text_refs
            )
            unsupported_numbers = sorted(
                _numeric_literals(claim.statement) - _numeric_literals(selected_anchor_text)
            )
            if unsupported_numbers:
                missing_components.append(
                    "Unverified quantitative detail(s): " + ", ".join(unsupported_numbers)
                )
                if verdict in {"direct", "synthesized"}:
                    verdict = "partial"
                claim.critic_flags.append(
                    "unverified_quantitative_literals:" + ",".join(unsupported_numbers)
                )
            claim.supported_rewrite = _compact(row.get("supported_rewrite"), 900)
            distinct_papers = {
                _paper_identity(text_map[ref]) for ref in text_refs if _paper_identity(text_map[ref])
            }
            if verdict == "synthesized" and (
                len(distinct_papers) < 2 or len(component_map) < 2 or missing_components
            ):
                verdict = "partial"
                claim.critic_flags.append("synthesized_evidence_requirements_not_met")

            # The source-side permission ceiling is authoritative.  A model
            # cannot turn a contextual, abstract, adjacent, or transfer source
            # into direct factual evidence simply by returning verdict=direct.
            if qualified_only and verdict in {"direct", "synthesized"}:
                verdict = "partial"
                claim.critic_flags.append("permission_ceiling_downgraded_verdict")

            # A model may return verdict=direct while every requested anchor
            # is discovery/background-only.  Emptying the ID list alone is
            # not enough: the lifecycle verdict itself must be downgraded so
            # downstream gates cannot mistake a rejected binding for evidence.
            if not text_refs and verdict not in {"contradicted"}:
                verdict = "insufficient"
            if verdict in {"insufficient", "contradicted"}:
                claim.supporting_text_chunk_ids = []
                claim.supporting_visual_chunk_ids = []
                claim.saturation_score = 0.0 if verdict == "contradicted" else min(float(claim.saturation_score), 0.5)
                claim.critic_flags.append(f"evidence_binding_{verdict}")
            else:
                claim.supporting_text_chunk_ids = [str(text_map[ref]["chunk_id"]) for ref in text_refs]
                claim.supporting_visual_chunk_ids = [str(visual_map[ref]["chunk_id"]) for ref in visual_refs]
                claim.critic_flags = [
                    flag for flag in claim.critic_flags
                    if flag not in {
                        "claim_unbound_after_generation",
                        "supporting_text_chunk_ids is empty; every claim needs text evidence",
                    }
                ]
                paper_count = len(distinct_papers)
                if verdict == "partial":
                    claim.saturation_score = {1: 0.75, 2: 1.1, 3: 1.3}.get(max(1, paper_count), 1.5)
                    claim.critic_flags.append("evidence_binding_partial")
                elif verdict == "synthesized":
                    claim.saturation_score = {2: 1.6, 3: 2.0}.get(max(2, paper_count), 2.3)
                    claim.critic_flags.append("evidence_binding_synthesized")
                else:
                    claim.saturation_score = min(2.8, 0.6 + 0.7 * max(1, paper_count))

            if abstract_only:
                # Abstract evidence can support a provisional/direct or
                # synthesized binding, but its saturation must stay bounded.
                claim.saturation_score = min(float(claim.saturation_score), 1.5)

            claim.evidence_binding_status = verdict
            if abstract_only:
                if confidence == "high":
                    confidence = "medium"
                claim.critic_flags.append("abstract_only_evidence_provisional")
                claim.evidence_binding_reason = _compact(
                    "[provisional abstract-only evidence] " + str(row.get("reason") or ""),
                    700,
                )
            else:
                claim.evidence_binding_reason = _compact(row.get("reason"), 700)
            claim.evidence_binding_confidence = confidence
            claim.evidence_synthesis_rationale = _compact(row.get("synthesis_rationale"), 900)
            claim.evidence_component_map = component_map
            claim.missing_evidence_components = missing_components
            spans: list[dict[str, str]] = []
            for span, ref, verified, relative_start, relative_end, match_mode in validated_span_rows:
                if not verified or ref not in text_map:
                    continue
                scope_fit = str(span.get("scope_fit") or "in_domain").strip().lower()
                if scope_fit not in {"in_domain", "cross_domain_analogy", "off_domain"}:
                    scope_fit = "in_domain"
                retrieval_role = str(
                    span.get("retrieval_role") or "evidence_candidate"
                ).strip().lower()
                if retrieval_role not in {
                    "evidence_candidate", "method_transfer", "background_only", "reject"
                }:
                    retrieval_role = "evidence_candidate"
                if scope_fit == "off_domain":
                    retrieval_role = "reject"
                elif scope_fit == "cross_domain_analogy" and retrieval_role == "evidence_candidate":
                    retrieval_role = "method_transfer"
                source_locator = dict(text_map[ref].get("source_locator") or {})
                if relative_start >= 0:
                    source_locator["relative_char_start"] = relative_start
                    source_locator["relative_char_end"] = relative_end
                spans.append({
                    "chunk_id": str(text_map[ref]["chunk_id"]),
                    "quote": _compact(span.get("quote"), 260),
                    "quote_translation": _compact(span.get("quote_translation"), 320),
                    "quote_verified": True,
                    "quote_match_mode": match_mode,
                    "source_locator": source_locator,
                    "scope_fit": scope_fit,
                    "retrieval_role": retrieval_role,
                    "raw_use_permission": text_map[ref].get("use_permission", DISCOVERY),
                    "permission_ceiling": permission_by_ref.get(ref, (DISCOVERY, "missing"))[0],
                    "content_depth": text_map[ref].get("content_depth", "metadata"),
                })
            claim.evidence_spans = spans
            # Keep lifecycle state synchronized with the latest verified
            # evidence.  A prior open question must not remain open after M3
            # establishes a direct or synthesized binding.
            if verdict in {"direct", "synthesized"} and text_refs:
                claim.claim_state = "grounded"
            elif verdict == "partial" and text_refs:
                claim.claim_state = "partially_grounded"
            elif verdict == "contradicted":
                claim.claim_state = "contested"
            elif verdict == "insufficient" and not text_refs:
                claim.claim_state = "open_question"
        self.last_audit["permission_audit"] = permission_audits
        self.last_audit["permission_rejected_count"] = len(permission_audits)
        return claims
