"""Opt-in bounded gap repair over existing retrieval and evidence consumers.

No model or paid API is implicitly constructed. Paid adapters must use the
shared reserve-before-HTTP client; the runner admits logical operations before
calling adapters and persists every attempt before dispatch. Query results are
navigation until a source loader supplies independently verified material.
"""
from __future__ import annotations
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path

from .candidate_corpus import QuerySpec, harvest_query, plan_queries
from .query_plan import provider_feedback
from .article_evidence import build_evidence_bundle, request_binding, validate_reader_result, is_budget_denial

SCHEMA = "review.coverage_repair.v1"
SCIENTIFIC_GAPS = {"zero_results", "expression_mismatch", "material_insufficient", "evidence_conflict", "unresolved"}


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def classify_gap(audit=None, *, material_ready=False, fulfilled=False, conflict=False, expression_mismatch=False):
    if fulfilled:
        return "satisfied"
    if audit and audit.get("execution_status") in {"failed", "partial"}:
        return "service_failure"
    if conflict:
        return "evidence_conflict"
    if expression_mismatch:
        return "expression_mismatch"
    if audit and audit.get("execution_status") == "zero_results":
        return "zero_results"
    return "unresolved" if material_ready else "material_insufficient"


def _merge_gap(previous, current):
    # A later empty channel or readable partial page cannot turn an unresolved
    # provider outage into evidence that the scientific query was inadequate.
    if current == "satisfied":
        return current
    if "service_failure" in (previous, current):
        return "service_failure"
    return current


class CoverageRepairRunner:
    """Stateful supplementary_search adapter consumed by Article's R04 service.

    facet_outputs explicitly maps facet IDs to required output IDs/descriptions.
    source_loader(hit=Hit, request=EvidenceRequest) resolves original material;
    it must not promote a search snippet to an original paper's SOURCE_UNIT.
    query_reviser, if supplied, receives only unmet facets and provider feedback
    and returns QuerySpecs carrying existing constraints and a parent query ID.
    Paid reader/reviser adapters require a shared budget_ledger declared here.
    """
    def __init__(self, *, plan, facet_outputs, gateway, source_loader, reader=None,
                 query_reviser=None, budget=None, state_path=None, budget_ledger=None,
                 paid_adapters=False):
        from .plan_contract import load_plan
        plan, self.plan_receipt = load_plan(plan)
        self.plan = plan
        self.mapping = {str(k): tuple(v) for k, v in facet_outputs.items()}
        self.specs = plan_queries(plan)
        known = {str(f["id"]) for f in plan.get("facets", [])}
        if set(self.mapping) != known or any(not v for v in self.mapping.values()):
            raise ValueError("complete_facet_output_mapping_required")
        self.gateway, self.loader, self.reader, self.reviser = gateway, source_loader, reader, query_reviser
        self.limits = {"max_rounds": 2, "max_queries": 6, "max_reads": 4, "max_transport_retries": 1,
                       "per_query_limit": 10, "max_revisions": 1, **(budget or {})}
        if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in self.limits.values()):
            raise ValueError("nonnegative_integer_repair_budgets_required")
        if paid_adapters and (budget_ledger is None or budget_ledger.limit_cny is None):
            raise ValueError("paid_adapters_require_shared_finite_ledger")
        self.ledger = budget_ledger
        self.path = Path(state_path) if state_path else None
        binding = _hash({"plan": plan, "mapping": self.mapping, "limits": self.limits})
        self.state = {"schema_version": SCHEMA, "binding": binding, "rounds": 0,
            "queries": 0, "reads": 0, "revisions": 0, "receipts": [], "units": [],
            "query_history": [], "attempted_reads": [], "stop_reason": "", "request_binding": None}
        if self.path and self.path.exists():
            self.state = json.loads(self.path.read_text())
            if self.state.get("binding") != binding or self.state.get("schema_version") != SCHEMA:
                raise ValueError("repair_checkpoint_binding_mismatch")
            # A crash cannot authorize redispatching a possibly billed operation.
            if any(r.get("status") == "started" for r in self.state["receipts"]):
                self.state["stop_reason"] = "uncertain_inflight_operation"
        for receipt in self.state["receipts"]:
            for addition in receipt.get("lineage", []):
                spec = QuerySpec(**addition["spec"])
                if not any(s.query_id == spec.query_id for s in self.specs):
                    self.specs.append(spec)
        self._emitted = set()
        self.remaining_capacity = None
        self._save()

    def supplement_with_capacity(self, *, request, unmet_outputs, remaining_papers):
        if not isinstance(remaining_papers, int) or isinstance(remaining_papers, bool) or remaining_papers < 0:
            raise ValueError("remaining_capacity_invalid")
        self.remaining_capacity = remaining_papers
        try:
            return self(request=request, unmet_outputs=unmet_outputs)
        finally:
            self.remaining_capacity = None

    def _emit(self):
        units = []
        for unit in self.state["units"]:
            key = (unit["canonical_paper_id"], unit["snapshot_id"], unit["source_hash"])
            if key not in self._emitted:
                units.append(unit)
                self._emitted.add(key)
        return units

    def _save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2))
            tmp.replace(self.path)

    def _admit(self, kind, **fields):
        counter, limit = {"query": ("queries", "max_queries"), "read": ("reads", "max_reads"),
                          "revision": ("revisions", "max_revisions")}[kind]
        if self.state[counter] >= self.limits[limit]:
            self.state["stop_reason"] = limit + "_reached"
            self._save()
            return None
        self.state[counter] += 1
        row = {"operation": kind, "round": self.state["rounds"], "status": "started", **fields}
        self.state["receipts"].append(row)
        self._save()
        return row

    def __call__(self, *, request, unmet_outputs):
        binding = request_binding(request)
        if self.state["request_binding"] not in (None, binding):
            raise ValueError("repair_request_binding_mismatch")
        self.state["request_binding"] = binding
        if not unmet_outputs:
            self.state["stop_reason"] = "coverage_satisfied"
            self._save()
            return []
        if set(unmet_outputs) - set(request.required_outputs):
            raise ValueError("repair_unknown_output")
        if set(x for values in self.mapping.values() for x in values) - set(request.required_outputs):
            raise ValueError("facet_mapping_unknown_output")
        if self.state["stop_reason"]:
            return self._emit()
        if self.limits["per_query_limit"] == 0:
            self.state["stop_reason"] = "per_query_limit_reached"
            self._save()
            return []
        new_units = []
        reads_at_entry = self.state["reads"]
        paper_cap = min(request.budget.get("max_papers", 8),
                        self.remaining_capacity if self.remaining_capacity is not None else request.budget.get("max_papers", 8))
        if paper_cap == 0:
            self.state["stop_reason"] = "remaining_paper_capacity_reached"
            self._save()
            return []
        remaining = set(unmet_outputs)
        while remaining and self.state["rounds"] < self.limits["max_rounds"]:
            self.state["rounds"] += 1
            facets = {k for k, v in self.mapping.items() if remaining.intersection(v)}
            before = len(self.state["units"])
            gaps = {}
            pending = [s for s in self.specs if s.facet_id in facets and
                       s.query_id not in {x["query_id"] for x in self.state["query_history"]}]
            for spec in pending:
                if not remaining.intersection(self.mapping[spec.facet_id]):
                    continue
                audit, hits = {}, []
                retained_hits = {}
                for retry in range(self.limits["max_transport_retries"] + 1):
                    receipt = self._admit("query", query_id=spec.query_id, facet_id=spec.facet_id,
                                          query_text=spec.query_text, transport_attempt=retry)
                    if receipt is None:
                        break
                    try:
                        hits, audit = harvest_query(self.gateway, spec, per_query_limit=self.limits["per_query_limit"])
                    except OSError as exc:
                        audit = {"execution_status": "failed", "error": type(exc).__name__, "returned": 0}
                    for hit in hits:
                        retained_hits[_hash({"identifiers": hit.identifiers, "row": hit.row})] = hit
                    receipt.update(status="finished", audit=audit)
                    self._save()
                    if audit.get("execution_status") not in {"failed", "partial"}:
                        break
                hits = list(retained_hits.values())
                self.state["query_history"].append({"query_id": spec.query_id, "spec": asdict(spec), "audit": audit})
                gaps[spec.facet_id] = _merge_gap(gaps.get(spec.facet_id), classify_gap(audit))
                for rank, hit in enumerate(hits, 1):
                    result_id = "repair-hit-" + _hash({"query_id": spec.query_id, "identifiers": hit.identifiers,
                                                       "row": hit.row, "rank": rank})[:24]
                    # Deduplicate acquisition by actual provider identity, while
                    # preserving every query/result receipt and original hit.
                    identity = _hash(hit.identifiers) if hit.identifiers else result_id
                    self.state["receipts"].append({"operation": "result", "status": "finished",
                        "round": self.state["rounds"], "query_id": spec.query_id, "result_id": result_id,
                        "provider_identifiers": hit.identifiers, "retrieval": hit.row})
                    if identity in self.state["attempted_reads"]:
                        continue
                    if self.state["reads"] - reads_at_entry >= paper_cap:
                        self.state["stop_reason"] = "remaining_paper_capacity_reached"
                        break
                    read = self._admit("read", result_id=result_id, provider_identifiers=hit.identifiers)
                    if read is None:
                        break
                    self.state["attempted_reads"].append(identity)
                    self._save()
                    unit = None
                    try:
                        unit = self.loader(hit=hit, request=request)
                        if unit:
                            provider_ids = {str(v) for v in hit.identifiers.values() if v}
                            linked = {str(v) for v in (unit.get("provider_identifiers") or {}).values() if v}
                            if str(unit.get("canonical_paper_id")) not in provider_ids and not provider_ids.intersection(linked):
                                raise ValueError("source_not_bound_to_retrieved_paper")
                        if unit and self.reader:
                            import copy
                            original = copy.deepcopy(unit)
                            unit = validate_reader_result(original, self.reader(request=request, source_unit=unit))
                        if unit:
                            checked = build_evidence_bundle(request, [unit])
                            unit = dict(checked.source_units[0])
                            unit["retrieval_result_id"] = result_id
                            key = (unit["canonical_paper_id"], unit["snapshot_id"], unit["source_hash"])
                            existing = {(u["canonical_paper_id"], u["snapshot_id"], u["source_hash"]) for u in self.state["units"]}
                            if key not in existing:
                                self.state["units"].append(unit)
                                new_units.append(unit)
                            remaining.difference_update(unit["fulfilled_outputs"])
                            gaps[spec.facet_id] = _merge_gap(gaps.get(spec.facet_id), classify_gap(audit, material_ready=checked.material_ready,
                                fulfilled=not remaining.intersection(self.mapping[spec.facet_id]),
                                conflict=bool(unit.get("counter_evidence")),
                                expression_mismatch=bool(unit.get("expression_mismatch"))))
                            read.update(status="finished", paper_id=unit["canonical_paper_id"],
                                snapshot_id=unit["snapshot_id"], fulfilled_outputs=unit["fulfilled_outputs"])
                        else:
                            read.update(status="material_insufficient")
                    except (RuntimeError, ValueError, OSError) as exc:
                        if is_budget_denial(exc):
                            read.update(status="not_dispatched_budget", error=str(exc))
                            self.state["stop_reason"] = "monetary_budget_reached"
                            if unit:
                                retained = dict(build_evidence_bundle(request, [unit]).source_units[0])
                                key = (retained["canonical_paper_id"], retained["snapshot_id"], retained["source_hash"])
                                if key not in {(u["canonical_paper_id"],u["snapshot_id"],u["source_hash"]) for u in self.state["units"]}:
                                    self.state["units"].append(retained)
                        elif isinstance(exc, RuntimeError):
                            raise
                        else:
                            read.update(status="unusable_material", error=type(exc).__name__)
                    self._save()
                    if self.state["stop_reason"] or not remaining.intersection(self.mapping[spec.facet_id]):
                        break
                if self.state["stop_reason"] or not remaining:
                    break
            self.state["receipts"].append({"operation": "gap", "status": "finished", "round": self.state["rounds"],
                "gaps": gaps, "unmet_outputs": sorted(remaining), "new_materials": len(self.state["units"]) - before})
            if not remaining:
                self.state["stop_reason"] = "coverage_satisfied"
            elif self.state["stop_reason"]:
                pass
            elif self.state["queries"] >= self.limits["max_queries"]:
                self.state["stop_reason"] = "max_queries_reached"
            elif self.state["reads"] >= self.limits["max_reads"]:
                self.state["stop_reason"] = "max_reads_reached"
            elif self.reviser and any(g in SCIENTIFIC_GAPS for g in gaps.values()) and self.state["rounds"] < self.limits["max_rounds"]:
                revise = self._admit("revision", facet_ids=sorted(k for k, g in gaps.items() if g in SCIENTIFIC_GAPS))
                if revise is not None:
                    feedback = provider_feedback([{"query": h["spec"]["query_text"], "facet": h["spec"]["facet_id"],
                        "provider_total": h["audit"].get("returned")} for h in self.state["query_history"]
                        if h["spec"]["facet_id"] in revise["facet_ids"] and h["audit"].get("execution_status") not in {"failed", "partial"}])
                    try:
                        replacements = self.reviser(plan=self.plan, unmet_facets=tuple(revise["facet_ids"]),
                                                   gaps=gaps, feedback=feedback)
                    except (RuntimeError, ValueError) as exc:
                        if not is_budget_denial(exc): raise
                        revise.update(status="not_dispatched_budget", error=str(exc))
                        self.state["stop_reason"] = "monetary_budget_reached"
                        self._save()
                        break
                    added = []
                    for proposal in replacements:
                        parent = next((s for s in self.specs if s.query_id == proposal.get("parent_query_id")), None)
                        spec = proposal.get("spec")
                        if (not isinstance(spec, QuerySpec) or parent is None or spec.facet_id != parent.facet_id
                            or spec.facet_id not in revise["facet_ids"] or not spec.query_text.strip()
                            or any(s.query_id == spec.query_id or (s.facet_id == spec.facet_id and s.query_text == spec.query_text) for s in self.specs)
                            or spec.query_type != parent.query_type or spec.retrieval_source != parent.retrieval_source
                            or spec.server_params != parent.server_params or spec.postfilters != parent.postfilters
                            or spec.unapplied_constraints != parent.unapplied_constraints):
                            continue
                        self.specs.append(spec)
                        added.append({"parent_query_id": parent.query_id, "spec": asdict(spec)})
                    revise.update(status="finished", lineage=added, feedback=feedback)
                    if not added:
                        self.state["stop_reason"] = "no_new_queries"
            elif len(self.state["units"]) == before:
                self.state["stop_reason"] = "no_new_effective_material"
            else:
                self.state["stop_reason"] = "unresolved_without_revision"
            self._save()
            if self.state["stop_reason"]:
                break
        if not self.state["stop_reason"]:
            self.state["stop_reason"] = "max_rounds_reached" if remaining else "coverage_satisfied"
        self.state["coverage"] = {k: {"required_outputs": list(v), "unmet_outputs": [x for x in v if x in remaining]}
                                  for k, v in self.mapping.items()}
        self._save()
        return self._emit()


def collect_evidence_with_repair(request, *, repair, index, source_resolver, directed_reader=None):
    """Actual R04 consumer: local FTS first, then opt-in bounded repair."""
    from .article_evidence import collect_evidence
    return collect_evidence(request, index=index, source_resolver=source_resolver,
                            directed_reader=directed_reader, supplementary_search=repair)
