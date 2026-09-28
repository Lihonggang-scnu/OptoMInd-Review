"""Upgrade-3 atomic claim <-> support-span binding (ticket 016).

Every claim binds atomically to its real support spans with its conditions and
canonical citation; the binding hash is the single identity that travels
Phase3 -> R3 -> R4 -> enhancer -> staged (one candidate transaction, no
per-stage setdefault divergence).

Binding rules:
- a support span is honoured only when it resolves (011 resolver), the text
  hash matches, the owner paper matches the claim, and the scope/permission of
  the source are legal for the claim role (012 effective permission);
- context spans are tracked separately and can NEVER substitute a missing core
  span, no matter how relevant;
- entailment (locally verified numbers + keyword support, with an optional
  independent-context LLM judgment) decides support; a verdict of uncertain
  leaves the claim unresolved;
- compound claims are split into atomic components; a half-supported claim is
  narrowed to its supported components as a NEW revision and re-audited —
  adding ``may``/``possibly`` never rescues an unsupported number;
- unresolved claims keep their original statement in the gap ledger and are
  fully separated from writable_claims.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Dict, Iterable, List, Optional

BINDINGS_SCHEMA = "optomind.upgrade3.claim_binding.v1"

_WS = re.compile(r"\s+")
_HEDGES = re.compile(r"(?i)\b(may|might|could|possibly|perhaps|arguably|presumably)\b")
_NUM = re.compile(r"\d+(?:\.\d+)?")


def _norm(text: str) -> str:
    import unicodedata
    folded = unicodedata.normalize("NFKC", text or "").translate(
        str.maketrans({chr(0x2010): '-', chr(0x2011): '-', chr(0x2012): '-',
                       chr(0x2013): '-', chr(0x2014): '-', chr(0x00A0): ' '}))
    return _WS.sub(" ", folded).strip().lower()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_atomic_components(statement: str) -> List[str]:
    """Split a compound statement into atomic components (semicolon/contrastive
    conjunctions only — conservative)."""
    parts = re.split(r";|\bwhereas\b|\bwhile\b(?! )|。|；", statement)
    out = [p.strip(" .;,") for p in parts if len(p.strip(" .;,")) >= 12]
    return out or [statement.strip()]


def numbers(statement: str) -> List[str]:
    return _NUM.findall(statement or "")


class ClaimBinder:
    def __init__(self, *, contract_hash: str, policy_hash: str,
                 effective_permission_fn: Callable[..., Dict[str, Any]],
                 entailment_fn: Optional[Callable[[str, str], str]] = None):
        """entailment_fn(statement, span_text) -> 'support'|'partial'|'contradict'
        |'not_enough_information' — run in an INDEPENDENT context (only the
        statement and the span are visible)."""
        self.contract_hash = contract_hash
        self.policy_hash = policy_hash
        self.permission_fn = effective_permission_fn
        self.entailment_fn = entailment_fn

    # -------------------------------------------------- span verification ----
    def verify_support_span(self, span: Dict[str, Any], claim: Dict[str, Any]) -> Optional[str]:
        """Returns None when the span is a legal core support, else the reason.
        Scope legality checked first; then owner/hash; then locator."""
        sid = span.get("span_id")
        if not span.get("text"):
            return f"core_span_unresolved:{sid}"
        scope = span.get("scope_verdict")
        if scope in ("out_of_scope", "uncertain"):
            return f"core_span_scope_illegal:{scope}"
        if not span.get("owner_paper_id") and not span.get("document_id"):
            return f"core_span_owner_missing:{sid}"
        if span.get("owner_paper_id") and claim.get("paper_id") \
                and _norm(span["owner_paper_id"]) != _norm(claim["paper_id"]) \
                and span.get("document_id") != claim.get("document_id"):
            return f"core_span_owner_mismatch:{sid}"
        if not span.get("text_sha256"):
            return f"core_span_hash_missing:{sid}"
        claimed_hash = span.get("text_sha256")
        if claimed_hash and claimed_hash != _sha(span["text"]):
            return f"core_span_hash_mismatch:{sid}"
        perm = self.permission_fn(
            source_depth=span.get("source_depth"),
            scope_verdict=scope,
            claim_role=claim.get("importance", "supporting"),
        )
        if not perm.get("writable"):
            return f"core_span_permission_insufficient:{perm['effective_permission']}"
        return None

    # -------------------------------------------------- entailment ----
    def local_entailment(self, statement: str, span_text: str) -> str:
        """Negative filter only: NEVER returns 'support'.  Local checks can
        detect clear mismatches (negation flip, missing numbers) and clear
        contradictions, but positive support requires the independent LLM
        entailment judge (entailment_fn)."""
        s_norm = _norm(statement)
        sp_norm = _norm(span_text)
        nums = numbers(s_norm)
        numbers_ok = all(n in sp_norm for n in nums) if nums else None
        # negation direction check: if statement negates but span affirms (or
        # vice versa), the overlap is misleading
        s_neg = bool(re.search(r"\b(not|no|never|without|lacks?|fails?|did not|"
                               r"does not|cannot|unable|reduced?|decreased?|"
                               r"degraded?|worse)\b", s_norm))
        sp_neg = bool(re.search(r"\b(not|no|never|without|lacks?|fails?|did not|"
                                r"does not|cannot|unable|reduced?|decreased?|"
                                r"degraded?|worse)\b", sp_norm))
        if s_neg != sp_neg and (nums or len(s_norm) > 30):
            return "contradict"  # negation direction flip
        toks = [t for t in re.split(r"[^a-z0-9]+", s_norm)
                if len(t) > 3 and t not in ('the', 'this', 'that', 'with',
                                            'from', 'have', 'been', 'were',
                                            'their', 'which', 'when', 'also')]
        if not toks:
            return "not_enough_information"
        overlap = sum(1 for t in toks if t in sp_norm) / len(toks)
        if nums and not numbers_ok:
            return "not_enough_information"  # numbers not backed by this span
        # NEVER returns 'support' — that requires the independent LLM judge
        return "not_enough_information"

    def entail(self, statement: str, span_text: str) -> str:
        if self.entailment_fn:
            try:
                verdict = self.entailment_fn(statement, span_text)
            except Exception:
                return "not_enough_information"
            if verdict in ("support", "partial", "contradict",
                           "not_enough_information"):
                return verdict
            return "not_enough_information"
        return self.local_entailment(statement, span_text)

    # -------------------------------------------------- binding ----
    def bind(self, claim: Dict[str, Any],
             support_spans: List[Dict[str, Any]],
             context_spans: List[Dict[str, Any]],
             required_components: Optional[List[str]] = None,
             revision: int = 1,
             citation_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        statement = claim["statement"]
        unresolved: List[str] = []
        verified_support: List[Dict[str, Any]] = []
        components = split_atomic_components(statement)
        component_support: Dict[str, str] = {}

        for span in support_spans:
            reason = self.verify_support_span(span, claim)
            if reason:
                unresolved.append(reason)
                continue
            verified_support.append(span)
        if not verified_support:
            unresolved.append("no_legal_core_support")
            for sid in claim.get("context_span_ids") or []:
                unresolved.append(f"context_cannot_replace_core:{sid}")

        # per-component entailment against verified spans
        for comp in components:
            best = "not_enough_information"
            for span in verified_support:
                v = self.entail(comp, span["text"])
                if v == "support":
                    best = "support"
                    break
                if v == "partial":
                    best = "partial"
            component_support[comp] = best

        supported_components = [c for c, v in component_support.items()
                                if v == "support"]
        partial_components = [c for c, v in component_support.items()
                              if v == "partial"]
        if required_components:
            for rc in required_components:
                if not any(rc.lower() in c.lower() for c in supported_components):
                    unresolved.append(f"required_component_unsupported:{rc}")

        if components and len(supported_components) < len(components):
            if supported_components:
                unresolved.append(
                    "compound_partially_supported:narrowed_to_"
                    + str(len(supported_components)))
            elif partial_components:
                unresolved.append("only_partial_support")
        contradicted = any(
            self.entail(comp, span["text"]) == "contradict"
            for comp in components for span in verified_support)

        if contradicted:
            binding_status = "invalid"
        elif verified_support and len(supported_components) == len(components) \
                and components:
            binding_status = "resolved"
        else:
            binding_status = "unresolved"

        perm = self.permission_fn(
            source_depth=claim.get("source_depth"),
            scope_verdict=claim.get("scope_verdict"),
            claim_role=claim.get("importance", "supporting"),
        )
        writable = (binding_status == "resolved"
                    and perm.get("writable") is True
                    and claim.get("scope_verdict") == "direct"
                    and not any("required_component_unsupported" in u
                                for u in unresolved))

        narrowed = None
        if supported_components and len(supported_components) < len(components):
            narrowed = {
                "narrowed_statement": " ".join(supported_components),
                "new_revision": revision + 1,
                "dropped_components": [c for c in components
                                       if c not in supported_components],
                "note": "unsupported components moved to gap ledger; re-audit required",
            }

        binding_body = {
            "claim_id": claim.get("claim_id"),
            "revision": revision,
            "statement": statement,
            "support_span_ids": [s.get("span_id") for s in verified_support],
            "context_span_ids": list(claim.get("context_span_ids") or []),
            "counter_span_ids": list(claim.get("counter_span_ids") or []),
            "required_components": required_components or [],
            "conditions": claim.get("conditions") or [],
            "scope_verdict": claim.get("scope_verdict"),
            "permission": perm["effective_permission"],
            "entailment": ("contradict" if contradicted
                           else ("support" if binding_status == "resolved"
                                 else ("partial" if partial_components
                                       else "not_enough_information"))),
            "binding_status": binding_status,
            "component_support": {k: v for k, v in component_support.items()},
            "unresolved_reasons": unresolved,
            "canonical_citation_ids": citation_ids or [],
            "narrowed_to": narrowed,
            "contract_hash": self.contract_hash,
            "policy_hash": self.policy_hash,
        }
        binding_hash = _sha(_canonical(binding_body))
        binding_body["binding_hash"] = binding_hash
        binding_body["writable"] = writable
        return {
            "schema_version": BINDINGS_SCHEMA,
            "binding": binding_body,
            "writable": writable,
            "gap": not writable,
            "gap_reasons": unresolved if not writable else [],
        }


def binding_transaction(claim_id: str, binding: Dict[str, Any],
                        evidence_rows: List[Dict[str, Any]],
                        citation_ids: List[str]) -> Dict[str, Any]:
    """One candidate transaction: statement + evidence + citation share the
    binding hash; adapters consume this object without regenerating state."""
    return {
        "schema_version": "optomind.upgrade3.binding_transaction.v1",
        "claim_id": claim_id,
        "binding_hash": binding["binding"]["binding_hash"],
        "statement_revision": binding["binding"]["revision"],
        "statement": binding["binding"]["statement"],
        "evidence_rows": evidence_rows,
        "citation_ids": citation_ids,
        "writable": binding["writable"],
    }


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
