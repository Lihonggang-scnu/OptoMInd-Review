"""Versioned Review→Article literature boundary, without a scientific compiler.

Search ranks discover material. Only explicitly fulfilled, source-anchored
outputs resolve a request. Literature claims are never ScientificClaims.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from copy import deepcopy
from hashlib import sha256
import json
from typing import Any, Mapping

VERSION = "review.evidence.v1"
MATERIAL_ROLES = frozenset({"problem_boundary", "model_mechanism", "parameter_prior",
                          "experiment_control", "counterexample_mismatch", "method_algorithm"})


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class EvidenceRequest:
    question: str
    object_conditions: Mapping[str, Any] = field(default_factory=dict)
    required_outputs: tuple[str, ...] = ()
    material_roles: tuple[str, ...] = ()
    current_gaps: tuple[str, ...] = ()
    budget: Mapping[str, Any] = field(default_factory=lambda: {"max_papers": 8, "max_supplementary_calls": 0})
    schema_version: str = VERSION

    def __post_init__(self):
        if self.schema_version != VERSION or not self.question.strip():
            raise ValueError("evidence_request_version_or_question_invalid")
        if set(self.material_roles) - MATERIAL_ROLES:
            raise ValueError("unknown_material_role")
        if not self.required_outputs or any(not isinstance(x, str) or not x.strip() for x in self.required_outputs):
            raise ValueError("required_outputs_must_be_nonempty_strings")
        for key in ("max_papers", "max_supplementary_calls"):
            value = self.budget.get(key, 8 if key == "max_papers" else 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("evidence_budget_invalid:" + key)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_mapping(cls, value):
        return cls(**{**value, **{key: tuple(value.get(key) or ()) for key in
                                ("required_outputs", "material_roles", "current_gaps")}})


@dataclass(frozen=True)
class EvidenceBundle:
    request: Mapping[str, Any]
    source_units: tuple[Mapping[str, Any], ...]
    unmet_outputs: tuple[str, ...]
    status: str
    material_ready: bool
    gap_fulfilled: bool
    task_id: str
    schema_version: str = VERSION
    claim_type: str = "LiteratureClaim"
    diagnostics: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_mapping(cls, value):
        if value.get("schema_version") != VERSION or value.get("claim_type") != "LiteratureClaim":
            raise ValueError("evidence_bundle_version_or_claim_type_invalid")
        rebuilt = build_evidence_bundle(EvidenceRequest.from_mapping(value["request"]), value["source_units"], diagnostics=value.get("diagnostics") or ())
        if _digest(rebuilt.to_dict()) != _digest(value):
            raise ValueError("evidence_bundle_integrity_mismatch")
        return rebuilt


def build_evidence_bundle(request, source_units, *, diagnostics=()):
    if not isinstance(request, EvidenceRequest):
        request = EvidenceRequest.from_mapping(request)
    units, fulfilled = [], set()
    for raw in source_units:
        unit = dict(raw)
        for key in ("canonical_paper_id", "snapshot_id", "source_hash", "material_depth"):
            if not isinstance(unit.get(key), str) or not unit[key].strip():
                raise ValueError("source_unit_missing:" + key)
        if len(unit["source_hash"]) != 64 or any(x not in "0123456789abcdef" for x in unit["source_hash"]):
            raise ValueError("source_hash_must_be_sha256")
        roles = list(unit.get("roles") or [])
        if not roles or set(roles) - MATERIAL_ROLES:
            raise ValueError("source_unit_roles_invalid")
        anchors = unit.get("anchors") or []
        for anchor in anchors:
            if not anchor.get("source_block_id") or not anchor.get("quote"):
                raise ValueError("source_anchor_incomplete")
            if anchor.get("snapshot_id", unit["snapshot_id"]) != unit["snapshot_id"]:
                raise ValueError("source_anchor_snapshot_mismatch")
        answers = unit.get("fulfilled_outputs") or []
        if set(answers) - set(request.required_outputs):
            raise ValueError("fulfilled_output_not_requested")
        if answers and not anchors:
            raise ValueError("fulfilled_output_requires_source_anchor")
        if answers and unit["material_depth"] in {"metadata_only", "navigation"}:
            raise ValueError("navigation_is_not_evidence")
        # Reader records must explicitly bind applicability to this request.
        if answers and unit.get("request_binding") != _digest(request.to_dict()):
            raise ValueError("fulfilled_output_request_binding_mismatch")
        unit.update(roles=roles, anchors=anchors, fulfilled_outputs=list(answers),
                    conditions=unit.get("conditions") or {}, counter_evidence=unit.get("counter_evidence") or [],
                    claim_type="LiteratureClaim")
        unit.pop("support_probability", None)
        fulfilled.update(answers)
        units.append(unit)
    unmet = tuple(x for x in request.required_outputs if x not in fulfilled)
    ready = any(u["anchors"] and u["material_depth"] not in {"metadata_only", "navigation"} for u in units)
    complete = not unmet
    return EvidenceBundle(request.to_dict(), tuple(units), unmet,
                          "gap_fulfilled" if complete else "partial" if ready else "unresolved",
                          ready, complete, "evidence-" + _digest({"request": request.to_dict(), "sources": sorted([{k: u[k] for k in ("canonical_paper_id", "snapshot_id", "source_hash")} for u in units], key=lambda u: (u["canonical_paper_id"], u["snapshot_id"]))}), diagnostics=tuple(diagnostics))


def request_binding(request):
    return _digest(request.to_dict() if isinstance(request, EvidenceRequest) else EvidenceRequest.from_mapping(request).to_dict())


def validate_reader_result(source, result):
    """Keep reader interpretations bound to the original material and quotes."""
    if not isinstance(result, Mapping):
        raise ValueError("reader_result_must_be_mapping")
    for key in ("canonical_paper_id", "snapshot_id", "source_hash", "material_depth"):
        if result.get(key) != source.get(key):
            raise ValueError("reader_source_identity_mismatch:" + key)
    blocks = {a["source_block_id"]: a for a in source.get("anchors", [])}
    for anchor in result.get("anchors", []):
        actual = blocks.get(anchor.get("source_block_id"))
        if not actual or not anchor.get("quote") or anchor["quote"] not in actual["quote"]:
            raise ValueError("reader_quote_not_in_source")
        for key in ("snapshot_id", "source_document_id", "source_locator"):
            if anchor.get(key) != actual.get(key):
                raise ValueError("reader_source_anchor_mismatch:" + key)
    return result


def _read_source(reader, request, source):
    # The callback receives its own copy: a failed or in-place-mutating reader
    # cannot corrupt the original material retained by the collector.
    return validate_reader_result(source, reader(request=request, source_unit=deepcopy(source)))


def is_budget_denial(exc):
    from .directed_reading import DirectedReadingError
    from .module4.runtime import QwenTransportError
    return isinstance(exc, (DirectedReadingError, QwenTransportError)) and str(exc) in {
        "global_budget_preflight_insufficient", "global_budget_exceeded"}


def collect_evidence(request, *, index, source_resolver, directed_reader=None, supplementary_search=None):
    """Use the existing local index and injected existing reader/service adapters.

    source_resolver returns SOURCE_UNIT data with snapshot block anchors. Search
    snippets alone cannot satisfy requirements. No live provider is constructed.
    supplementary_search is called only within the explicitly supplied budget.
    """
    from .planning_material_search import search
    if not isinstance(request, EvidenceRequest):
        request = EvidenceRequest.from_mapping(request)
    limit = request.budget.get("max_papers", 8)
    hits = search(index, request.question, top_papers=limit).hits if limit else []
    units, seen, diagnostics = [], set(), []
    budget_stopped = False
    for hit in hits:
        if hit.paper_id in seen or len(seen) >= limit:
            continue
        seen.add(hit.paper_id)
        try:
            unit = source_resolver(hit)
        except OSError as exc:
            diagnostics.append({"stage": "source_resolver", "paper_id": hit.paper_id, "error": type(exc).__name__})
            continue
        if unit is None:
            continue
        if directed_reader:
            try:
                unit = _read_source(directed_reader, request, unit)
            except OSError as exc:
                diagnostics.append({"stage": "directed_reader", "paper_id": hit.paper_id, "error": type(exc).__name__})
            except (RuntimeError, ValueError) as exc:
                if not is_budget_denial(exc): raise
                diagnostics.append({"stage": "directed_reader", "paper_id": hit.paper_id, "reason": "monetary_budget_reached"})
                budget_stopped = True
        units.append(unit)
        if budget_stopped: break
    bundle = build_evidence_bundle(request, units, diagnostics=diagnostics)
    for _ in range(request.budget.get("max_supplementary_calls", 0)):
        if budget_stopped or bundle.gap_fulfilled or supplementary_search is None:
            break
        if len(seen) >= limit:
            break
        try:
            capacity_call = getattr(supplementary_search, "supplement_with_capacity", None)
            if capacity_call is not None:
                additional = capacity_call(request=request, unmet_outputs=bundle.unmet_outputs,
                                           remaining_papers=limit - len(seen))
            elif seen:
                # A legacy callback only knows the full request cap. Once local
                # reads consumed capacity it cannot safely infer the remainder.
                diagnostics.append({"stage": "supplementary_search", "status": "pending",
                                    "reason": "remaining_capacity_contract_required"})
                bundle = build_evidence_bundle(request, units, diagnostics=diagnostics)
                break
            else:
                additional = supplementary_search(request=request, unmet_outputs=bundle.unmet_outputs)
        except OSError as exc:
            diagnostics.append({"stage": "supplementary_search", "error": type(exc).__name__})
            bundle = build_evidence_bundle(request, units, diagnostics=diagnostics)
            break
        if not additional:
            break
        for unit in additional:
            pid = unit.get("canonical_paper_id")
            if pid in seen:
                continue
            if len(seen) >= limit:
                break
            seen.add(pid)
            if directed_reader:
                try:
                    unit = _read_source(directed_reader, request, unit)
                except OSError as exc:
                    diagnostics.append({"stage": "directed_reader", "paper_id": unit.get("canonical_paper_id"), "error": type(exc).__name__})
                except (RuntimeError, ValueError) as exc:
                    if not is_budget_denial(exc): raise
                    diagnostics.append({"stage": "directed_reader", "paper_id": unit.get("canonical_paper_id"), "reason": "monetary_budget_reached"})
                    budget_stopped = True
            units.append(unit)
            if budget_stopped: break
        bundle = build_evidence_bundle(request, units, diagnostics=diagnostics)
    return bundle


def research_brief_input(bundle):
    """C04 owns ResearchBrief compilation; this is its lossless input preview."""
    if not isinstance(bundle, EvidenceBundle):
        bundle = EvidenceBundle.from_mapping(bundle)
    return {"evidence_interface": VERSION, "evidence_bundle": bundle.to_dict(),
            "scientific_truth_certified": False, "ranking_is_support_strength": False}


def source_unit_from_snapshot(*, snapshot_dir, roles, conditions=None, block_ids=None, counter_evidence=()):
    """Load immutable source bytes through the existing trusted snapshot provider."""
    from .local_materials import PreparedSnapshotProvider
    from .paper_reading_card import _sha, _snapshot_payload
    snapshot = PreparedSnapshotProvider(snapshot_dir).load()
    packet = snapshot.build_reading_packet()
    wanted = set(block_ids) if block_ids is not None else None
    anchors = []
    seen = set()
    for block in snapshot.blocks:
        ident = block.get("block_id")
        if wanted is not None and ident not in wanted:
            continue
        text = block.get("text_normalized") or block.get("text_raw") or ""
        if text.strip():
            anchors.append({"source_block_id": ident, "snapshot_id": snapshot.manifest["snapshot_id"],
                            "quote": text, "source_document_id": block.get("source_document_id"),
                            "source_locator": block.get("source_locator") or block.get("locator")})
            seen.add(ident)
    if wanted is not None and wanted - seen:
        raise ValueError("requested_source_block_missing")
    return {"canonical_paper_id": snapshot.manifest["canonical_paper_id"],
            "snapshot_id": snapshot.manifest["snapshot_id"], "snapshot_dir": str(snapshot.root),
            "source_hash": _sha(_snapshot_payload(snapshot, packet)),
            "material_depth": packet["material_scope"], "roles": list(roles),
            "conditions": conditions or {}, "anchors": anchors,
            "counter_evidence": list(counter_evidence), "fulfilled_outputs": []}


def read_evidence_source(*, request, source_unit, store, output_dir, client=None, **reader_options):
    """Actual directed-reading consumer, with its existing task/cache/admission rules.

    Practical-reader answers remain interpretations. Fulfillment is deliberately
    not inferred from paper availability or a reader's nonempty answer; anchored
    requirement assessment can be supplied by the caller separately.
    """
    from .directed_reading import run_directed_reading, _fulltext_scope
    if not isinstance(request, EvidenceRequest):
        request = EvidenceRequest.from_mapping(request)
    if not _fulltext_scope(source_unit["material_depth"]):
        return {**source_unit, "reader_status": "insufficient_fulltext", "reader_reused": False}
    binding = request_binding(request)
    review_id = "article-evidence-" + binding[:24]
    paper = {"canonical_paper_id": source_unit["canonical_paper_id"],
             "title": source_unit.get("title", ""), "material_scope": source_unit["material_depth"],
             "snapshot_dir": source_unit["snapshot_dir"], "nomination_reason": request.question,
             "required_outputs": list(request.required_outputs), "approve_core": True,
             "expected_information_gain": request.question, "core_justification": "Requested evidence outputs require source reading",
             "knowledge_gap": "; ".join(request.required_outputs)}
    store.admit_candidate(review_id, binding, paper)
    outputs = [{"output_id": f"O{i:02d}", "output_type": "evidence", "description": value} for i, value in enumerate(request.required_outputs, 1)]
    from .evidence_assessment import assessment_instructions, apply_output_assessments
    task = {"questions": [{"question_id": "Q01", "question": request.question,
                           "purpose": json.dumps(assessment_instructions(request, source_unit), ensure_ascii=False),
                           "required_output_ids": [o["output_id"] for o in outputs]}],
            "required_outputs": outputs, "gap_keys": list(request.current_gaps)}
    result = run_directed_reading(request={"review_id": review_id, "topic_binding": binding, **task},
        paper=paper, snapshot_dir=source_unit["snapshot_dir"], output_dir=output_dir,
        store=store, client=client, **reader_options)
    interpreted = {**source_unit, "reader_interpretation": result.get("output"),
            "directed_reading_path": result.get("output_dir"), "reader_reused": result.get("reused", False),
            "reader_retained": result.get("retained", False), "reader_network_call": result.get("network_call", False),
            "request_binding": binding}

    content = ((result.get("output") or {}).get("content") or {})
    rows = content.get("output_assessments", []) if isinstance(content, Mapping) else []
    return apply_output_assessments(request, interpreted, rows)
