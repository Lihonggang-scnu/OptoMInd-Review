"""Production wiring factories for upgrade3 decision gates (031 W2-W11).

Each factory builds an upgrade3 decision object from the run's own context —
the frozen domain contract written by the entry (W1), the persistent budget
ledger (005), the production transport (006) and the configured Qwen
credentials — so the harness call sites can attach real producers without
duplicating configuration logic.  Nothing in here fabricates model output:
when the judge cannot run, callers must fail closed (candidate stays
non-direct) rather than fall back to lexical promotion.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from config.qwen_config import get_qwen_client_config

DEFAULT_POLICY_HASH = "upgrade3-default-policy-v1"


def ledger_path() -> Path:
    root = Path(os.environ.get("OPTOMIND_UPGRADE3_ROOT")
                or Path(__file__).resolve().parents[3])
    return root / "outputs" / "upgrade3" / "BUDGET_LEDGER.sqlite"


def run_budget_task_id(default: str = "031") -> str:
    """The upgrade3 task whose pool pays for the current run's real calls."""
    return os.environ.get("OPTOMIND_UPGRADE3_BUDGET_TASK", default)


def load_domain_contract(run_dir: Path) -> Optional[Dict[str, Any]]:
    """Read the frozen W1 contract; None when missing or needs_scope."""
    path = Path(run_dir) / "DOMAIN_CONTRACT.json"
    if not path.is_file():
        return None
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(contract, dict):
        return None
    if contract.get("status") == "needs_scope":
        return None
    if not contract.get("envelope", {}).get("content_sha256"):
        return None
    return contract


def build_transport(policy_hash: str = DEFAULT_POLICY_HASH):
    from optomind_research.runtime.upgrade3 import budget_ledger as B
    from optomind_research.runtime.upgrade3 import provider_transport as T

    root = ledger_path().parents[2]
    pricing_path = root / "config" / "model_pricing.json"
    pricing = json.loads(pricing_path.read_text(encoding="utf-8"))
    ledger = B.BudgetLedger(str(ledger_path()), pricing=pricing)
    cache_root = str(root / "outputs" / "upgrade3" / "RESPONSE_CACHE")
    return T.ProductionTransport(ledger, cache_root=cache_root,
                                 policy_hash=policy_hash)


def qwen_key_candidates() -> List[str]:
    cfg = get_qwen_client_config("b_plus")
    return [str(k["api_key"]) for k in cfg.get("api_key_candidates", [])
            if k.get("api_key")]


def qwen_base_url() -> str:
    cfg = get_qwen_client_config("b_plus")
    return str(cfg.get("base_url") or "")


class RotatingScopeAdmission:
    """A ScopeAdmission wrapper with bounded key rotation.

    The primary configured key may be unpaid/dead (400 Arrearage classifies
    as auth_error and the transport freezes the reservation conservatively).
    Mirrors the 010 real-test pattern: rotate to the next candidate on
    auth-class errors, defer on transient transport loss."""

    def __init__(self, *, keys: List[str], make_admission, max_passes: int = 3):
        self._keys = keys
        self._make = make_admission
        self._max_passes = max_passes
        self._idx = 0
        self._current = make_admission(keys[0])
        self.rotation_events: List[str] = []

    def rotate(self, reason: str) -> bool:
        if self._idx + 1 >= len(self._keys):
            return False
        self._idx += 1
        self.rotation_events.append(f"key#{self._idx}:{reason[:80]}")
        self._current = self._make(self._keys[self._idx])
        return True

    def decide(self, paper: Dict[str, Any], logical_call_id: str) -> Dict[str, Any]:
        from optomind_research.runtime.upgrade3.provider_transport import (
            TransportError,
        )
        last_exc: Optional[Exception] = None
        for _pass in range(self._max_passes):
            try:
                return self._current.decide(paper, logical_call_id)
            except TransportError as exc:
                last_exc = exc
                if exc.error_class == "auth_error":
                    if not self.rotate(str(exc)):
                        raise
                    continue
                # transient transport loss: bounded defer, same key
                continue
        raise last_exc  # type: ignore[misc]


def build_scope_admission(*, run_dir: Path, work_dir: Path,
                          task_id: Optional[str] = None,
                          generation_id: Optional[str] = None,
                          contract: Optional[Dict[str, Any]] = None,
                          policy_hash: str = DEFAULT_POLICY_HASH):
    """A real 010 ScopeAdmission bound to this run's contract/ledger/keys.

    Returns None when the run has no usable frozen contract (the entry would
    have stopped at W1) — callers must treat None as "gate cannot grant
    direct", never as "skip review".  Keys rotate with bounds on auth-class
    failures so a single dead key cannot demote the whole candidate pool.
    """
    from optomind_research.runtime.upgrade3.scope_screening import (
        ScopeAdmission,
        ScopeDecisionLog,
    )

    contract = contract or load_domain_contract(run_dir)
    if contract is None:
        return None
    keys = qwen_key_candidates()
    if not keys:
        return None
    transport = build_transport(policy_hash)
    log_path = Path(work_dir) / "upgrade3_scope_decisions.jsonl"

    def make_admission(key: str) -> ScopeAdmission:
        return ScopeAdmission(
            log=ScopeDecisionLog(str(log_path)),
            transport=transport,
            contract=contract,
            policy_hash=policy_hash,
            model="qwen3.7-flash",
            api_key=key,
            base_url=qwen_base_url(),
            task_id=task_id or run_budget_task_id(),
            generation_id=generation_id or Path(run_dir).name,
            judge_system="scope judge",
            max_input_tokens=4000,
            max_output_tokens=4000,
        )

    return RotatingScopeAdmission(keys=keys, make_admission=make_admission)


# 010 verdict -> legacy scope_fit vocabulary.  uncertain maps to unreviewed
# (candidate-only); background maps to contextual; rejected maps to
# out_of_scope.  Only reviewed_direct can satisfy the factual lane downstream.
VERDICT_TO_SCOPE_FIT = {
    "reviewed_direct": "direct",
    "adjacent": "adjacent",
    "background": "contextual",
    "rejected": "out_of_scope",
    "uncertain": "unreviewed",
}


# ---------------------------------------------------------------- W4 (016) --

ROLE_ALIASES = {
    "method": "methods", "methods": "methods",
    "architecture": "architectures", "architectures": "architectures",
    "training": "training", "training_method": "training",
    "application": "applications", "applications": "applications",
    "mechanism": "mechanism", "mechanisms": "mechanism",
    "comparison": "comparison", "comparisons": "comparison",
    "bottleneck": "bottlenecks", "bottlenecks": "bottlenecks",
    "future_direction": "future_directions",
    "future_directions": "future_directions",
    "limitation": "bottlenecks", "limitations": "bottlenecks",
}


def normalize_role(role: str) -> str:
    return ROLE_ALIASES.get(str(role or "").strip().casefold(),
                            str(role or "").strip().casefold())


ENTAIL_SYSTEM = (
    "You are an independent entailment judge. You see exactly one atomic "
    "claim statement and one candidate support passage. Decide whether the "
    "passage, by itself, entails the statement. Answer with JSON only: "
    '{"entailment": "support|partial|contradict|not_enough_information"}. '
    "support requires every number and condition in the statement to be "
    "present in the passage; contradict means the passage asserts the "
    "opposite direction; otherwise not_enough_information.")


class EntailmentBudget(Exception):
    pass


def production_entailment_fn(transport, *, task_id: str, generation_id: str,
                             max_calls: int = 1200,
                             max_output_tokens: int = 2000):
    """An independent-context entailment judge over the production transport.

    The judge sees only the statement and the span — never the author's
    reasoning.  Fail-closed: any error or unparseable output is
    not_enough_information; the call budget is hard-capped.  Identical
    (statement, span) pairs are served from a local cache so the binder's
    component×span expansion does not multiply provider calls.
    """
    calls = {"n": 0}
    keys = qwen_key_candidates()
    key_idx = {"i": 0}
    cache: dict = {}

    def entail(statement: str, span_text: str) -> str:
        cache_key = (statement, span_text)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        calls["n"] += 1
        if calls["n"] > max_calls:
            return "not_enough_information"
        import json as _json
        from optomind_research.runtime.upgrade3.scope_screening import (
            _strip_json_fence,
        )
        from optomind_research.runtime.upgrade3.provider_transport import (
            TransportError,
        )
        response = None
        for _attempt in range(2):
            try:
                response = transport.chat_completion(
                    task_id=task_id,
                    logical_call_id=f"entail:{calls['n']}",
                    generation_id=generation_id, model="qwen3.7-flash",
                    system_prompt=ENTAIL_SYSTEM,
                    user_payload={"statement": statement, "support_passage": span_text,
                                  "output_schema": {"entailment": "str"}},
                    api_key=keys[key_idx["i"]], base_url=qwen_base_url(),
                    schema={"entailment": "str"},
                    max_input_tokens=3000, max_output_tokens=max_output_tokens,
                    temperature=0.0)
                break
            except TransportError as exc:
                if (exc.error_class == "auth_error"
                        and key_idx["i"] + 1 < len(keys)):
                    key_idx["i"] += 1
                    continue
                return "not_enough_information"
            except Exception:
                return "not_enough_information"
        if response is None:
            return "not_enough_information"
        try:
            data = _json.loads(_strip_json_fence(response["content"]))
            verdict = str(data.get("entailment") or "")
        except Exception:
            return "not_enough_information"
        verdict = verdict if verdict in ("support", "partial", "contradict",
                                         "not_enough_information") \
            else "not_enough_information"
        cache[cache_key] = verdict
        return verdict

    entail.call_count = calls  # type: ignore[attr-defined]
    return entail


def build_claim_binder(run_dir: Path, *, contract=None,
                       policy_hash: str = DEFAULT_POLICY_HASH,
                       entailment_fn=None):
    """A real 016 ClaimBinder bound to the run contract + 012 permission fn.

    The 012 caps hold structured_snippet sources at ``reported_only`` PENDING
    claim-level audit; the 016 binding IS that audit (atomic statement +
    verified span + independent entailment).  The wrapper therefore lifts a
    direct-scoped, context-complete structured snippet to
    ``qualified_support`` — the contract's audited-snippet lane.  abstract /
    metadata depths stay capped; nothing else is lifted."""
    from optomind_research.runtime.upgrade3.claim_binding import ClaimBinder
    from optomind_research.runtime.upgrade3.evidence_permission import (
        effective_permission,
    )

    def audited_snippet_permission(*, source_depth, scope_verdict,
                                   claim_role="supporting"):
        perm = effective_permission(
            source_depth=source_depth, scope_verdict=scope_verdict,
            claim_role=claim_role)
        if (perm.get("effective_permission") == "reported_only"
                and source_depth == "structured_snippet"
                and scope_verdict == "direct"):
            perm = dict(perm,
                        effective_permission="qualified_support",
                        writable=True,
                        reasons=list(perm.get("reasons") or [])
                        + ["upgrade3_016_audited_snippet_uplift:direct+"
                           "structured_snippet+claim_level_entailment"])
        return perm

    contract = contract or load_domain_contract(run_dir)
    if contract is None:
        return None
    return ClaimBinder(
        contract_hash=str(contract["envelope"]["content_sha256"]),
        policy_hash=policy_hash,
        effective_permission_fn=audited_snippet_permission,
        entailment_fn=entailment_fn)


# ------------------------------------------------- SM04 batched entailment --


BATCH_PROMPT_SCHEMA_VERSION = "optomind.upgrade3.batched_entailment.prompt.v1"
BATCH_OUTPUT_SCHEMA_VERSION = "optomind.upgrade3.batched_entailment.output.v1"


def entailment_prompt_hash(system_prompt: str, schema_version: str) -> str:
    """The prompt identity a cache entry is keyed by."""

    import hashlib

    return hashlib.sha256(
        (str(system_prompt) + "\u0000" + str(schema_version)).encode("utf-8")
    ).hexdigest()


def entailment_schema_hash(schema_version: str) -> str:
    import hashlib

    return hashlib.sha256(str(schema_version).encode("utf-8")).hexdigest()


def build_entailment_pairs(
    *,
    atomic_claims: List[Dict[str, Any]],
    atoms_by_id: Dict[str, Dict[str, Any]],
    prompt_hash: str = "",
    schema_hash: str = "",
) -> List[Dict[str, Any]]:
    """One pair per (claim, atom span) the claim actually declares.

    The pair set is built before any model call, so the topology is a plan the
    receipt can be compared against.  A claim whose atoms cannot be resolved
    contributes no pair and is reported by the binder as a gap instead.
    """

    # The cache identity must be identical between the run that judged the pairs and
    # the run that later replays them, so when the caller does not supply the prompt
    # identity it is derived from the module contract rather than left empty (an
    # empty value would silently make every entry a different key).
    from optomind_research.runtime.upgrade3 import batched_entailment as _batch_module

    if not prompt_hash:
        prompt_hash = entailment_prompt_hash(
            _batch_module.BATCH_SYSTEM, BATCH_PROMPT_SCHEMA_VERSION
        )
    if not schema_hash:
        schema_hash = entailment_schema_hash(BATCH_OUTPUT_SCHEMA_VERSION)

    pairs: List[Dict[str, Any]] = []
    for claim in atomic_claims:
        if not isinstance(claim, Mapping):
            continue
        claim_id = str(claim.get("claim_id") or "")
        statement = str(claim.get("atomic_statement") or "").strip()
        if not claim_id or not statement:
            continue
        for index, atom_id in enumerate(claim.get("atom_ids") or []):
            atom = atoms_by_id.get(str(atom_id))
            if not isinstance(atom, Mapping):
                continue
            span = dict(atom.get("span") or {})
            span_text = str(
                span.get("quote")
                or ((atom.get("field") or {}).get("value"))
                or "",
            ).strip()
            if not span_text:
                continue
            pairs.append({
                "pair_id": "%s::%s" % (claim_id, str(span.get("span_id") or index)),
                "claim_id": claim_id,
                "atom_id": str(atom_id),
                "span_id": str(span.get("span_id") or ""),
                "statement": statement,
                "span_text": span_text,
                "prompt_hash": str(prompt_hash or ""),
                "schema_hash": str(schema_hash or ""),
            })
    return pairs


def bind_atomic_section_claims_batched(
    *,
    section_id: str,
    atomic_claims: List[Dict[str, Any]],
    atoms_by_id: Dict[str, Dict[str, Any]],
    binder_factory,
    generation_id: str,
    cache_path: Path,
    judge=None,
    batch_size: int = 12,
    max_batch_calls: int = 12,
    model: str = "qwen3.7-flash",
    policy_sha256: str = "",
    material_snapshot_hash: str = "",
    schema_hash: str = "",
    prompt_hash: str = "",
    task_id: str = "031",
    max_claims: int = 32,
    entailment_fn=None,
) -> Dict[str, Any]:
    """Bind a section evidence-first, judging entailment in batches first.

    Order of operations matters: the pair plan is built, the deterministic
    prefilter drops what cannot be entailed, the remaining pairs are judged in
    batches (cache first), and only then does the binder run - fed by a receipt
    lookup that cannot trigger a provider call from inside the binding loop.
    """

    from optomind_research.runtime.upgrade3 import batched_entailment as _batched

    pairs = build_entailment_pairs(
        atomic_claims=atomic_claims,
        atoms_by_id=atoms_by_id,
        prompt_hash=prompt_hash,
        schema_hash=schema_hash,
    )
    cache = _batched.EntailmentCache(cache_path)

    def _no_provider(*_args, **_kwargs):
        raise _batched.BatchedEntailmentError("provider_call_forbidden_in_this_pass")

    # A pass without a judge still resolves everything the cache already holds -
    # that is exactly the resume case - and can only reach the provider when a
    # judge was supplied.  A pair that is neither cached nor judgeable stays
    # unevaluated rather than being silently treated as unsupported.
    receipt = _batched.judge_pairs(
        pairs,
        judge=judge if judge is not None else _no_provider,
        cache=cache,
        batch_size=batch_size,
        max_batch_calls=0 if judge is None else max_batch_calls,
        prompt_model=model,
        policy_sha256=policy_sha256,
        material_snapshot_hash=material_snapshot_hash,
        task_id=task_id,
        generation_id=generation_id,
    )

    verdict_fn = _batched.receipt_verdict_fn(receipt)
    binder = binder_factory(verdict_fn if entailment_fn is None else entailment_fn)
    bindings = bind_atomic_section_claims(
        section_id=section_id,
        atomic_claims=atomic_claims,
        atoms_by_id=atoms_by_id,
        binder=binder,
        generation_id=generation_id,
        max_claims=max_claims,
    )
    return {
        "bindings": bindings,
        "entailment_receipt": receipt,
        "planned_pairs": len(pairs),
        "provider_calls": int(receipt.get("provider_calls") or 0),
        "cache_hits": int(receipt.get("cache_hits") or 0),
        "single_pair_equivalent_calls": int(
            receipt.get("single_pair_equivalent_calls") or 0
        ),
    }

def _declared_support_ids(claim: Dict[str, Any]) -> Dict[str, List[str]]:
    """Minimal phase3 claim-role chunk extraction (kept local to avoid a
    circular import with the phase3 orchestrator)."""

    def values(*fields: str) -> List[str]:
        out: List[Any] = []
        for field_name in fields:
            raw = claim.get(field_name)
            if isinstance(raw, (list, tuple, set, frozenset)):
                out.extend(raw)
            elif raw not in (None, ""):
                out.append(str(raw))
        seen: set = set()
        return [item for item in out
                if not (item in seen or seen.add(item))]  # type: ignore[func-returns-value]

    return {
        "positive_support": values(
            "supporting_text_chunk_ids", "supporting_chunk_ids",
            "support_chunk_ids", "direct_support_chunk_ids",
            "factual_support_chunk_ids"),
        "context_and_reported": values(
            "context_text_chunk_ids", "contextual_support_chunk_ids",
            "context_support_chunk_ids", "author_reported_support_chunk_ids",
            "author_reported_chunk_ids"),
    }


EVIDENCE_TYPE_ROLES = {
    "mechanism": "mechanism",
    "method": "methods",
    "application": "applications",
    "comparison": "comparison",
    "measurement": "comparison",
    "training": "training",
    "bottleneck": "bottlenecks",
    "limitation": "bottlenecks",
    "result": "architectures",
    "architecture": "architectures",
}


def bind_section_claims(*, section_id: str, claims: List[Dict[str, Any]],
                        chunk_records_by_id: Dict[str, Dict[str, Any]],
                        binder, generation_id: str,
                        max_claims: int = 64) -> List[Dict[str, Any]]:
    """Bind one section's final claims through the 016 binder.

    claims: phase3 claim dicts (statement + declared support chunk ids).
    chunk_records_by_id: _graph_record-shaped records (text/scope_fit/
    content_depth/paper_id).  Spans are constructed here from the SAME
    records the claim declared, with text_sha256 verified by the binder.
    """
    import hashlib as _hashlib

    results: List[Dict[str, Any]] = []
    for claim in claims[:max_claims]:
        statement = str(claim.get("statement")
                        or claim.get("claim") or "").strip()
        if len(statement) < 12:
            continue
        role_ids = _declared_support_ids(claim)
        support_ids = role_ids["positive_support"]
        context_ids = role_ids["context_and_reported"]
        # Binding roles come only from this claim's declarations and its
        # controlled evidence_type mapping.  Section required_roles remain a
        # downstream coverage requirement and cannot be binding answers.
        evidence_type = str(claim.get("evidence_type") or "").strip().casefold()
        mapped_evidence_role = EVIDENCE_TYPE_ROLES.get(evidence_type)
        evidence_roles = ({normalize_role(mapped_evidence_role)}
                          if mapped_evidence_role else set())
        roles = sorted(
            (evidence_roles
             | {normalize_role(r) for r in (claim.get("literature_roles")
                                            or claim.get("roles") or [])})
            - {""})
        spans: List[Dict[str, Any]] = []
        for sid in support_ids + context_ids:
            record = chunk_records_by_id.get(sid)
            if not record:
                continue
            text = str(record.get("text") or record.get("normalized_text") or "")
            if not text:
                continue
            spans.append({
                "span_id": sid,
                "text": text,
                "text_sha256": _hashlib.sha256(
                    text.encode("utf-8")).hexdigest(),
                "scope_verdict": str(record.get("scope_fit") or "unreviewed"),
                "owner_paper_id": str(record.get("paper_id") or ""),
                "source_depth": str(record.get("content_depth") or "metadata"),
            })
        support = spans[:len(support_ids)] or []
        context = spans[len(support_ids):]
        # the claim rides on its declared support: the direct lane requires a
        # direct-scoped support span (post-W2 these are judge-reviewed)
        support_scopes = [s["scope_verdict"] for s in support]
        claim_scope = ("direct" if support_scopes
                       and all(s == "direct" for s in support_scopes)
                       else (support_scopes[0] if support_scopes else "uncertain"))
        claim_input = {
            "claim_id": str(claim.get("claim_id") or f"{section_id}:c"),
            "statement": statement,
            "paper_id": str(claim.get("paper_id") or ""),
            "importance": str(claim.get("importance")
                              or claim.get("evidence_type") or "supporting"),
            "context_span_ids": [s["span_id"] for s in context],
            "conditions": claim.get("conditions") or [],
            "scope_verdict": claim_scope,
            "source_depth": (support[0]["source_depth"] if support else
                             (context[0]["source_depth"] if context
                              else "metadata")),
        }
        try:
            bound = binder.bind(claim_input, support, context,
                                required_components=list(
                                    claim.get("required_components") or []),
                                citation_ids=list(dict.fromkeys(
                                    s["owner_paper_id"] for s in support)))
        except Exception as exc:
            bound = {
                "schema_version": "optomind.upgrade3.claim_binding.v1",
                "binding": {"claim_id": claim_input["claim_id"],
                            "statement": statement,
                            "binding_status": "unresolved",
                            "writable": False,
                            "unresolved_reasons": [
                                f"binder_error:{type(exc).__name__}"],
                            "roles": roles},
                "writable": False, "gap": True,
                "gap_reasons": [f"binder_error:{type(exc).__name__}"],
            }
        body = dict(bound["binding"], roles=roles)
        bound = dict(bound, binding=body)
        # audit trace outside binding_body (never part of binding_hash): the
        # 027 final science audit checks sentence numbers against THESE span
        # texts, so a bound number is always traceable to legal support.
        bound["span_texts"] = {
            s["span_id"]: s["text"][:2000]
            for s in support + context
        }
        results.append(bound)
    return results


# ------------------------------------------------- W4 + SM02 atomic claims --


def bind_atomic_section_claims(*, section_id: str,
                               atomic_claims: List[Dict[str, Any]],
                               atoms_by_id: Dict[str, Dict[str, Any]],
                               binder, generation_id: str,
                               max_claims: int = 32) -> List[Dict[str, Any]]:
    """Bind evidence-first atomic claims through the 016 binder.

    Unlike the legacy path this never searches for support: the claim already
    declares the atoms it came from, and the span handed to the binder is the one
    the atom producer verified against the document version.  A claim whose atom
    or span cannot be resolved is skipped with a visible audit row rather than
    being bound against anything else.
    """
    import hashlib as _hashlib

    results: List[Dict[str, Any]] = []
    for claim in atomic_claims[:max_claims]:
        if not isinstance(claim, Mapping):
            continue
        statement = str(claim.get("atomic_statement") or "").strip()
        claim_id = str(claim.get("claim_id") or "")
        atom_ids = [str(item) for item in (claim.get("atom_ids") or [])]
        if len(statement) < 12 or not claim_id or not atom_ids:
            results.append(_atomic_binding_gap(
                claim_id=claim_id or f"{section_id}:c",
                statement=statement,
                role_ids=[],
                reason="atomic_claim_incomplete",
            ))
            continue
        atoms = [atoms_by_id.get(atom_id) for atom_id in atom_ids]
        if any(atom is None for atom in atoms):
            results.append(_atomic_binding_gap(
                claim_id=claim_id,
                statement=statement,
                role_ids=atom_ids,
                reason="atomic_claim_atom_not_found",
            ))
            continue
        spans: List[Dict[str, Any]] = []
        for atom in atoms:
            span = dict(atom.get("span") or {})
            text = str(span.get("quote") or ((atom.get("field") or {}).get("value")) or "")
            if not text:
                spans = []
                break
            spans.append({
                "span_id": str(span.get("span_id") or ""),
                "text": text,
                "text_sha256": _hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "scope_verdict": str(
                    (atom.get("role_provenance") or {}).get("scope_verdict")
                    or "unreviewed"),
                "owner_paper_id": str(span.get("paper_id") or ""),
                "source_depth": str(
                    span.get("content_depth")
                    or (atom.get("role_provenance") or {}).get("source_depth")
                    or "metadata"),
                # the atom-verified canonical provenance, carried into the binding
                "canonical_span_id": str(span.get("span_id") or ""),
                "document_id": str(
                    (span.get("source_provenance") or {}).get("document_id") or ""),
                "pdf_page": span.get("pdf_page"),
                "char_start": span.get("char_start"),
                "char_end": span.get("char_end"),
            })
        if not spans or any(not span["span_id"] for span in spans):
            results.append(_atomic_binding_gap(
                claim_id=claim_id,
                statement=statement,
                role_ids=atom_ids,
                reason="atomic_claim_span_unresolved",
            ))
            continue
        scope_verdicts = [span["scope_verdict"] for span in spans]
        claim_scope = ("direct" if scope_verdicts
                       and all(value == "direct" for value in scope_verdicts)
                       else (scope_verdicts[0] if scope_verdicts else "uncertain"))
        condition_anchors = [str(item) for item in (claim.get("condition_anchors") or [])]
        claim_input = {
            "claim_id": claim_id,
            "statement": statement,
            "paper_id": str(claim.get("paper_id") or ""),
            "importance": str(claim.get("importance") or "supporting"),
            "context_span_ids": [],
            "conditions": condition_anchors,
            "scope_verdict": claim_scope,
            "source_depth": spans[0]["source_depth"],
        }
        roles = sorted({
            normalize_role(role)
            for role in (claim.get("role_provenance") or {}).get("roles") or ()
            if normalize_role(role)
        })
        mapped = EVIDENCE_TYPE_ROLES.get(
            str(claim.get("evidence_type") or "").strip().casefold())
        if mapped:
            roles = sorted(set(roles) | {normalize_role(mapped)} - {""})
        try:
            bound = binder.bind(claim_input, spans, [],
                                required_components=list(
                                    claim.get("required_components") or []),
                                citation_ids=list(dict.fromkeys(
                                    span["owner_paper_id"] for span in spans)))
        except Exception as exc:
            results.append(_atomic_binding_gap(
                claim_id=claim_id,
                statement=statement,
                role_ids=atom_ids,
                reason=f"binder_error:{type(exc).__name__}",
            ))
            continue
        body = dict(bound["binding"], roles=roles)
        body["atom_ids"] = list(atom_ids)
        body["canonical_span_ids"] = [span["span_id"] for span in spans]
        # Conditions travel with the binding: a binding that lost its conditions
        # must not be treated as writable evidence.
        body["condition_ids"] = list(claim.get("condition_ids") or [])
        body["condition_atom_ids"] = list(claim.get("condition_atom_ids") or [])
        body["condition_anchors"] = list(claim.get("condition_anchors") or [])
        body["canonical_envelope"] = dict(claim.get("envelope") or {})
        body["atomic_statement"] = statement
        # The role a binding carries must be traceable to the evidence that gave
        # it the role, not just asserted on the binding.
        body["role_provenance"] = dict(claim.get("role_provenance") or {})
        # Where the evidence physically is: the coverage receipt must be able to
        # send a reader from a covered role to the page of the document it rests on.
        body["document_ids"] = list(dict.fromkeys(
            str(span.get("document_id") or "") for span in spans
            if str(span.get("document_id") or "")
        ))
        body["pdf_pages"] = [
            span.get("pdf_page") for span in spans if span.get("pdf_page") is not None
        ]
        body["char_ranges"] = [
            [span.get("char_start"), span.get("char_end")] for span in spans
        ]
        # The canonical fields above are added AFTER the binder computed its
        # hash, so the hash must be recomputed over the extended body or the W4
        # authority projection refuses the row.  The scheme is the binder's:
        # roles and writable are attached after the hash and are not covered.
        body.pop("binding_hash", None)
        _hashed = {
            key: value for key, value in body.items()
            if key not in ("binding_hash", "roles", "writable")
        }
        body["binding_hash"] = _hashlib.sha256(
            json.dumps(_hashed, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        bound = dict(bound, binding=body)
        bound["span_texts"] = {span["span_id"]: span["text"][:2000] for span in spans}
        bound["atomic_claim_id"] = claim_id
        results.append(bound)
    return results


def _atomic_binding_gap(*, claim_id: str, statement: str, role_ids, reason: str) -> Dict[str, Any]:
    """A fail-closed binding row for an atomic claim that cannot be bound."""

    import hashlib as _hashlib

    # A gap row is still a W4 row: it must carry every field the authority
    # projection requires, or the whole section's bindings are refused.
    body = {
        "claim_id": claim_id,
        "statement": statement,
        "binding_status": "unresolved",
        "writable": False,
        "unresolved_reasons": [reason],
        "support_span_ids": [],
        "context_span_ids": [],
        "counter_span_ids": [],
        "permission": "unbound",
    }
    body["binding_hash"] = _hashlib.sha256(
        json.dumps({key: value for key, value in body.items()
                    if key not in ("binding_hash", "roles", "writable")},
                   ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    body["roles"] = []
    return {
        "schema_version": "optomind.upgrade3.claim_binding.v1",
        "binding": body,
        "writable": False,
        "gap": True,
        "gap_reasons": [reason],
        "span_texts": {},
        "atomic_claim_id": claim_id,
        "declared_atom_ids": list(role_ids or []),
    }

# ---------------------------------------------------------------- W3 (017) --

def build_coverage_verdicts(*, states: List[Dict[str, Any]],
                            bindings_by_section: Dict[str, List[Dict[str, Any]]],
                            generation_id: str,
                            demoted_roles: Optional[Dict[str, List[str]]] = None,
                            adaptation_hash: str = "",
                            revision_view: Optional[Dict[str, Any]] = None,
                            ) -> Dict[str, Dict[str, Any]]:
    """A 017 verdict per section from required roles + the W4 bindings.

    ``demoted_roles`` (from the blueprint-literature adaptation) removes
    roles from the required-slot set: the plan's half yielded to the
    literature's half for roles with zero article-wide supply; question
    slots are never demoted upstream."""
    from optomind_research.runtime.upgrade3.coverage_readiness import (
        coverage_verdict,
    )

    # revision_view is the frozen blueprint revision (SM08) read through that
    # module's own consumer entry point: it is the single path by which the
    # literature reshapes a section's role set, and its hash travels into every
    # verdict.  The legacy demoted_roles/adaptation_hash pair is honoured only
    # when no revision exists, so one decision never has two producers, and a
    # question-required slot is never demoted by either path.
    revision_sections: Dict[str, Any] = {}
    revision_hash = ""
    if revision_view:
        revision_hash = str(revision_view.get("revision_hash") or "")
        revision_sections = dict(revision_view.get("sections") or {})
        if str(revision_view.get("generation_id") or "") not in ("", generation_id):
            raise ValueError("blueprint_revision_generation_mismatch")
    verdicts: Dict[str, Dict[str, Any]] = {}
    for state in states:
        section = state.get("section") or {}
        section_id = str(section.get("section_id") or "")
        demoted_here = {normalize_role(r) for r in
                        (demoted_roles or {}).get(section_id) or []}
        revised_section = revision_sections.get(section_id) or {}
        declared_roles = (
            revised_section.get("required_roles")
            if revision_view and revised_section
            else section.get("required_roles") or []
        )
        required_roles = sorted({normalize_role(r) for r in (
            declared_roles or [])} - {""} - demoted_here)
        slots = [{"slot_id": f"role:{role}",
                  "requirement_id": f"{section_id}:{role}",
                  "role": role, "required": True}
                 for role in required_roles]
        bindings = bindings_by_section.get(section_id) or []
        verdict = coverage_verdict(
            section_id=section_id, generation_id=generation_id,
            required_slots=slots, bindings=bindings)
        if revision_view:
            revised_mounts = list(revised_section.get("revision_mounts") or [])
            verdict = dict(
                verdict,
                blueprint_revision={
                    "revision_hash": revision_hash,
                    "soft_roles": sorted(revised_section.get("soft_roles") or []),
                    "comparison_axes": sorted(
                        revised_section.get("comparison_axes") or []),
                    "subsections": [
                        str(item.get("subsection_id") or "")
                        for item in revised_section.get("subsections") or []
                        if isinstance(item, dict)
                    ],
                    "revision_mount_rows": len(revised_mounts),
                    "revision_mount_chunks": sum(
                        len(item.get("chunk_ids") or [])
                        for item in revised_mounts if isinstance(item, dict)
                    ),
                    "grants_permission": False,
                },
            )
        if demoted_here or adaptation_hash or revision_hash:
            verdict = dict(verdict, notes=list(verdict.get("notes") or []) + (
                [f"blueprint_revision:{revision_hash[:16]}"]
                if revision_hash else [
                    f"blueprint_adaptation:{adaptation_hash[:16]}",
                    f"demoted_roles:{sorted(demoted_here)}",
                ]
            ))
            import hashlib as _h
            verdict["verdict_hash"] = _h.sha256(json.dumps(
                {k: v for k, v in verdict.items() if k != "verdict_hash"},
                sort_keys=True, ensure_ascii=False,
                default=str).encode("utf-8")).hexdigest()
        verdicts[section_id] = verdict
    return verdicts
