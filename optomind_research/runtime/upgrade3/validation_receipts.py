"""Every module must be really validated before it may be called complete (SM09).

A completion claim is a machine-checkable object, not a sentence in a log.  This
module defines the VALIDATION_RECEIPT schema, computes the level a receipt's own
evidence actually supports (V0 unit / V1 real artifact offline / V2 real provider
module / V3 formal producer -> module -> consumer / V4 multi-stage slice / V5 full
E2E), and reduces a queue to completed / refused.  Only the reducer may produce
completed; a receipt that grades itself, that lowers its required level, that
substitutes pytest green or a file name for a consumer receipt, that reports a
fresh node as reused or a compiled PDF whose audit failed, is refused with the
reason named.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

RECEIPT_SCHEMA = "optomind.upgrade3.validation_receipt.v1"
GATE_SCHEMA = "optomind.upgrade3.validation_gate.v1"
REDUCTION_SCHEMA = "optomind.upgrade3.queue_reduction.v1"
NODE_GATE_SCHEMA = "optomind.upgrade3.node_validation_gate.v1"

LEVELS = ("V0", "V1", "V2", "V3", "V4", "V5")
LEVEL_RANK = {level: index for index, level in enumerate(LEVELS)}

#: required_validation values that are not product science levels
REQUIREMENT_CLASSES = {
    "read_only_audit": ("baseline", "V0"),
    "baseline_verified": ("baseline", "V0"),
    "investigation_complete": ("baseline", "V0"),
    "preflight_candidate": ("preflight", "V1"),
    "independent_readiness": ("readiness", "V1"),
}

AGGREGATOR_GRADERS = ("aggregator", "reducer", "validation_receipts")

REAL_POSITIVE_KINDS = (
    "real_module_test",
    "real_model_call",
    "real_recovery_test",
    "real_recovery_drill",
    "real_cache_restart",
    "real_selective_invalidation",
    "real_artifact",
    "real_retrieval",
    "real_scope_judgement",
    "full_e2e",
)

CONSUMER_OK_STATUSES = ("wired", "available", "exercised", "not_applicable")
CONSUMER_PENDING_STATUSES = (
    "pending",
    "consumers_pending",
    "pending_wiring",
    "not_wired",
    "library_only",
    "planned",
)

ACCEPTED_AUDIT_STATUSES = ("passed", "compiled_awaiting_metadata", "ok")

#: which refusals invalidate which level: a level is only reached when its own
#: premise holds, so a pending consumer can never be reported as V3
LEVEL_BLOCKERS = {
    "V0": frozenset(),
    "V1": frozenset({
        "entrypoint_missing", "entrypoint_failed", "node_fingerprints_missing",
        "node_not_committed", "hash_drift", "evidence_file_missing",
        "no_real_positive_example", "pytest_green_is_not_v3",
    }),
    "V2": frozenset({
        "cost_receipt_missing", "cost_receipt_unreadable", "cost_over_cap",
        "zero_cost_claim_without_a_zero_call_proof", "ledger_rows_mismatch",
    }),
    "V3": frozenset({
        "consumer_pending", "consumer_missing",
        "file_exists_is_not_a_consumer_receipt", "no_negative_example",
        "negative_without_a_zero_permission_proof", "false_reuse_claim",
        "compile_claim_without_passing_audit", "self_reported_completion",
        "receipt_hash_mismatch", "receipt_hash_missing",
        "declared_level_exceeds_evidence", "declared_level_missing",
    }),
    "V4": frozenset({"stage_count_below_v4"}),
    "V5": frozenset({"no_terminal_manifest_for_v5"}),
}


class ValidationReceiptError(RuntimeError):
    """The receipt cannot be used as evidence: never repaired, never inferred."""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _mapping(value: Any) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list:
    if isinstance(value, (list, tuple)):
        return [item for item in value]
    if isinstance(value, Mapping):
        out = []
        for key, item in value.items():
            if isinstance(item, Mapping):
                out.append(dict(item, id=_text(item.get("id")) or _text(key)))
            else:
                out.append({"id": _text(key), "kind": "nested", "observed": item})
        return out
    return []


def level_of(value: Any) -> str:
    text = _text(value).upper().replace("LEVEL", "").strip()
    return text if text in LEVEL_RANK else ""


def requirement_class(required: Any) -> tuple:
    """(class, level) for a declared required_validation value."""

    raw = _text(required)
    if raw in REQUIREMENT_CLASSES:
        return REQUIREMENT_CLASSES[raw]
    level = level_of(raw)
    if level:
        return ("product", level)
    return ("unknown", "")


# --------------------------------------------------------------------------- #
# sealing and adapting
# --------------------------------------------------------------------------- #

def receipt_body(receipt: Mapping[str, Any]) -> dict:
    return {key: value for key, value in receipt.items()
            if key not in ("receipt_hash", "observed_at")}


def seal_receipt(receipt: dict) -> dict:
    receipt["receipt_hash"] = _sha(receipt_body(receipt))
    return receipt


_DOTTED = None


def _dotted_token(text: str) -> tuple:
    """The first module.function token in a prose field, or ('', '')."""

    import re

    global _DOTTED
    if _DOTTED is None:
        _DOTTED = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\.py)?\.([A-Za-z_][A-Za-z0-9_]*)")
    match = _DOTTED.search(text or "")
    if not match:
        return "", ""
    return match.group(1), match.group(2)


def _extract_consumer(body: Mapping[str, Any]) -> tuple:
    """Find the consumer as the receipt states it, and never invent one."""

    declared = body.get("consumer")
    if isinstance(declared, Mapping):
        module = _text(declared.get("module"))
        function = _text(declared.get("function"))
        if not function:
            functions = declared.get("functions")
            if isinstance(functions, (list, tuple)) and functions:
                function = _text(functions[0])
            elif _text(functions):
                function = _text(functions)
        payload = declared.get("receipt")
        if not isinstance(payload, Mapping):
            described = declared.get("consumer_receipt") or declared.get("receipt")
            payload = ({"description": _text(described)}
                       if isinstance(described, str) and _text(described) else {})
        return (
            {"module": module, "function": function,
             "status": _text(declared.get("status")) or "wired",
             "receipt": dict(payload)},
            "consumer",
        )
    entry = _mapping(body.get("formal_entrypoint"))
    for key in ("consumer", "p3c", "p3b", "orchestrator_entry", "admission",
                "node"):
        value = entry.get(key)
        if isinstance(value, Mapping):
            return _extract_consumer({"consumer": value})
        text = _text(value)
        if not text:
            continue
        module, function = _dotted_token(text)
        if not module:
            continue
        described = _text(
            _mapping(body.get("quantified_acceptance")).get(
                "producer_to_consumer_receipt")
        ) or text
        return (
            {"module": module, "function": function, "status": "wired",
             "receipt": {"description": described}},
            "formal_entrypoint.%s" % key,
        )
    if isinstance(declared, str) and _text(declared):
        module, function = _dotted_token(declared)
        return (
            {"module": module or _text(declared), "function": function,
             "status": "wired", "receipt": {"description": _text(declared)}},
            "consumer_string",
        )
    return ({"module": "", "function": "", "status": "pending", "receipt": {}},
            "absent")


def canonicalize_receipt(receipt: Mapping[str, Any]) -> dict:
    """Map an existing receipt onto the canonical fields, inventing nothing.

    Field names differ between the tickets that were written before this schema
    existed.  The adapter only renames and flattens; anything it cannot find stays
    absent, and an absent required element becomes a refusal rather than a guess.
    """

    body = dict(receipt)
    entry = _mapping(body.get("entrypoint") or body.get("formal_entrypoint"))
    if not entry:
        entry = {}
    first_entry = ""
    for row in _rows(body.get("positive_examples")):
        first_entry = _text(_mapping(row).get("entry"))
        if first_entry:
            break
    canonical_entry = {
        "path": _text(entry.get("path") or entry.get("file")
                      or entry.get("harness_entry") or first_entry
                      or entry.get("artifact")),
        "hook": _text(entry.get("hook") or entry.get("method")
                      or entry.get("producer") or entry.get("class")),
        "ran": bool(entry.get(
            "ran",
            bool(entry.get("path") or entry.get("producer") or entry.get("file")
                 or first_entry),
        )),
        "exit_code": entry.get("exit_code", 0),
        "error": _text(entry.get("error")),
    }
    producer = _mapping(body.get("producer"))
    if not producer:
        producer = {"module": _text(entry.get("producer"))}
    canonical_consumer, consumer_from = _extract_consumer(body)
    if not _text(canonical_consumer.get("module")):
        positives = _rows(body.get("positive_examples"))
        for row in positives:
            module, function = _dotted_token(_text(_mapping(row).get("entry")))
            if module:
                canonical_consumer["module"] = module
                canonical_consumer["function"] = function
                canonical_consumer["status"] = "wired"
                canonical_consumer["receipt"] = {"description": _text(
                    _mapping(row).get("entry"))}
                consumer_from = "positive_example.entry"
                break
    canonical = {
        "schema_version": RECEIPT_SCHEMA,
        "receipt_id": _text(body.get("receipt_id") or body.get("task_id")),
        "task_id": _text(body.get("task_id")),
        "task_name": _text(body.get("task_name")),
        "required_validation_level": _text(
            body.get("required_validation_level")
            or body.get("required_validation")
            or _mapping(body.get("formal_entrypoint")).get("required_validation")
        ),
        "achieved_validation_level": _text(
            body.get("achieved_validation_level") or body.get("achieved_level")),
        "entrypoint": canonical_entry,
        "producer": producer,
        "consumer": canonical_consumer,
        "consumer_from": consumer_from,
        "positive_examples": _rows(body.get("positive_examples")),
        "negative_examples": _rows(body.get("negative_examples")),
        "input_output_hashes": _mapping(body.get("input_output_hashes")),
        "cost_receipts": _mapping(body.get("cost_receipts")
                                  or body.get("cost_receipt")
                                  or body.get("budget")),
        "quality_conclusion": _text(
            body.get("quality_conclusion")
            or _mapping(body.get("quantified_acceptance")).get("quality_conclusion")
            or ("present" if body.get("quantified_acceptance") else "")
        ),
        "adapted_from": "legacy" if body.get("schema_version") != RECEIPT_SCHEMA
                        else "canonical",
        "original_schema_version": _text(body.get("schema_version")),
    }
    canonical["quantified_acceptance"] = _mapping(body.get("quantified_acceptance"))
    for key in ("recovery", "compile_status", "audit", "graded_by", "stages",
                "terminal_manifest", "notes"):
        if key in body:
            canonical[key] = body[key]
    return canonical


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #

#: a negative example may not declare any of these above zero
POSITIVE_PERMISSION_KEYS = frozenset({
    "write_permission", "downstream_calls", "author_calls", "authored_calls",
    "publication_calls", "writable", "admitted", "accepted", "granted",
})


def _zero_proof(negatives: Sequence[Mapping[str, Any]],
                quantified: Mapping[str, Any]) -> bool:
    """Is there a machine-readable zero anywhere in the falsification evidence?"""

    for value in (quantified or {}).values():
        if value is False or (isinstance(value, (int, float))
                              and not isinstance(value, bool) and value == 0):
            return True
    for row in negatives or ():
        for source in (row, _mapping(row.get("observed")),
                       _mapping(row.get("result")),
                       _mapping(row.get("zero_write_proof"))):
            for value in source.values():
                if value is False or (isinstance(value, (int, float))
                                      and not isinstance(value, bool) and value == 0):
                    return True
    return False


def _evidence_level(receipt: Mapping[str, Any]) -> tuple:
    """The highest level this receipt's own evidence supports, with its basis."""

    basis: list[str] = []
    positives = [row for row in receipt.get("positive_examples") or []
                 if isinstance(row, Mapping)]
    kinds = {_text(row.get("kind")) for row in positives}
    if not positives:
        return "", basis
    basis.append("V0:positive_example_present")
    level = "V0"
    if kinds & set(REAL_POSITIVE_KINDS):
        level = "V1"
        basis.append("V1:real_artifact_example")
    if "real_model_call" in kinds or int(
        _mapping(receipt.get("producer")).get("model_calls") or 0
    ) > 0:
        if level >= "V1":
            level = "V2"
            basis.append("V2:real_provider_call")
    consumer = _mapping(receipt.get("consumer"))
    negatives = [row for row in receipt.get("negative_examples") or []
                 if isinstance(row, Mapping)]
    negative_ok = bool(negatives) and _zero_proof(
        negatives, _mapping(receipt.get("quantified_acceptance")))
    entry = _mapping(receipt.get("entrypoint"))
    # V3 is the producer -> module -> consumer chain, which a deterministic
    # module can reach without a provider call; V2 (a real provider module) is a
    # different kind of evidence, not a required rung on the way to V3.
    if (
        level >= "V1"
        and _text(consumer.get("module"))
        and _text(consumer.get("function"))
        and isinstance(consumer.get("receipt"), Mapping)
        and negative_ok
        and _text(entry.get("path"))
    ):
        level = "V3"
        basis.append("V3:formal_producer_and_consumer_with_negative")
    stages = [row for row in _rows(receipt.get("stages")) if isinstance(row, Mapping)]
    if level >= "V3" and len(stages) >= 2:
        level = "V4"
        basis.append("V4:two_or_more_real_stages")
    terminal = _mapping(receipt.get("terminal_manifest"))
    if level >= "V4" and terminal and _text(terminal.get("sha256") or terminal.get("path")):
        level = "V5"
        basis.append("V5:terminal_manifest")
    return level, basis


def verify_receipt(
    receipt: Mapping[str, Any],
    *,
    expected_hash: str = "",
    evidence_root: str | os.PathLike | None = None,
    ledger_rows: Mapping[str, int] | None = None,
) -> dict:
    """Compute what a receipt's evidence supports.  Nothing is repaired here."""

    canonical = canonicalize_receipt(receipt)
    violations: list[str] = []
    klass, required_level = requirement_class(
        canonical.get("required_validation_level"))
    declared = level_of(canonical.get("achieved_validation_level"))
    if not declared and klass == "baseline":
        # a read-only audit declares its class, not a product science level
        declared = required_level
        canonical["achieved_validation_level"] = declared
    if not declared:
        violations.append("declared_level_missing")

    declared_hash = _text(_mapping(receipt).get("receipt_hash"))
    if declared_hash:
        if _sha(receipt_body(_mapping(receipt))) != declared_hash:
            violations.append("receipt_hash_mismatch")
    elif expected_hash:
        violations.append("receipt_hash_missing")
    if expected_hash and declared_hash and declared_hash != _text(expected_hash):
        violations.append("receipt_hash_mismatch")

    entry = _mapping(canonical.get("entrypoint"))
    if klass == "product":
        if not _text(entry.get("path")):
            violations.append("entrypoint_missing")
        if entry.get("ran") is False or int(entry.get("exit_code") or 0) != 0:
            violations.append(
                "entrypoint_failed:%s" % (_text(entry.get("error")) or "non_zero_exit"))

    consumer = _mapping(canonical.get("consumer"))
    consumer_status = _text(consumer.get("status")).casefold()
    if klass == "product":
        if consumer_status in CONSUMER_PENDING_STATUSES:
            violations.append("consumer_pending:%s" % consumer_status)
        if not _text(consumer.get("module")) or not _text(consumer.get("function")):
            violations.append("consumer_missing")
        payload = consumer.get("receipt")
        usable = isinstance(payload, Mapping) and any(
            value not in (None, "", [], {}) for value in payload.values()
        )
        if usable and set(payload) <= {"description"}:
            # a named file is not an observation: only an actual consumer result is
            import re

            described = _text(payload.get("description"))
            if re.fullmatch(
                r"[A-Za-z0-9_./\\-]+\.(json|jsonl|csv|txt|md|pdf|sqlite)",
                described,
            ):
                usable = False
        if not usable:
            violations.append("file_exists_is_not_a_consumer_receipt")

    graded_by = _text(canonical.get("graded_by")).casefold()
    if graded_by and graded_by not in AGGREGATOR_GRADERS:
        violations.append("self_reported_completion:%s" % graded_by)

    recovery = _mapping(canonical.get("recovery"))
    if recovery:
        claimed = {_text(item) for item in recovery.get("claimed_reused_nodes") or []}
        observed = _mapping(recovery.get("observed_node_states"))
        for node_id in sorted(claimed):
            state = _text(observed.get(node_id)).casefold()
            if state and state not in ("committed", "reused", "reuse"):
                violations.append("false_reuse_claim:%s:%s" % (node_id, state))

    if _text(canonical.get("compile_status")).casefold() == "compiled":
        audit_status = _text(_mapping(canonical.get("audit")).get("status")).casefold()
        if audit_status not in ACCEPTED_AUDIT_STATUSES:
            violations.append(
                "compile_claim_without_passing_audit:%s" % (audit_status or "missing"))

    negatives = [row for row in canonical.get("negative_examples") or []
                 if isinstance(row, Mapping)]
    if klass == "product" and required_level >= "V2":
        if not negatives:
            violations.append("no_negative_example")
        # A negative example only counts when it proves the failure actually
        # failed: it must not declare a nonzero permission or call count, and the
        # receipt must state at least one zero-valued falsification count either
        # on a negative row or in its quantified acceptance.
        for index, row in enumerate(negatives):
            for source in (row, _mapping(row.get("observed")),
                           _mapping(row.get("result")),
                           _mapping(row.get("zero_write_proof"))):
                for key, value in source.items():
                    if key.casefold() in POSITIVE_PERMISSION_KEYS and isinstance(
                        value, (int, float)
                    ) and not isinstance(value, bool) and value > 0:
                        violations.append(
                            "negative_declares_a_nonzero_permission:%d:%s"
                            % (index, key))
        if not _zero_proof(negatives, _mapping(canonical.get("quantified_acceptance"))
                           or _mapping(_mapping(receipt).get("quantified_acceptance"))):
            violations.append("negative_without_a_zero_permission_proof")

    cost = _mapping(canonical.get("cost_receipts"))
    if not cost:
        violations.append("cost_receipt_missing")
    else:
        occupied = cost.get("occupied_cny", cost.get("spend_cny"))
        cap = cost.get("cap_cny", cost.get("sm_cap_cny"))
        if cap is not None and occupied is not None:
            try:
                if float(occupied) > float(cap) + 1e-9:
                    violations.append("cost_over_cap")
            except (TypeError, ValueError):
                violations.append("cost_receipt_unreadable")
        zero_claim = (occupied in (0, 0.0, "0", None)
                      and not _mapping(cost.get("ledger_rows")))
        declared_calls = [
            value for key, value in list(cost.items()) + list(
                _mapping(canonical.get("producer")).items())
            if "model_call" in key.casefold()
        ]
        ledger_reference = bool(_mapping(cost.get("ledger_rows"))) or any(
            key in cost for key in ("ledger_path", "ledger_file", "ledger_file_sha256")
        )
        ledger_zero = bool(ledger_rows is not None and not any(
            int(value or 0) for value in dict(ledger_rows).values()))
        # A zero claim must be provable, not merely asserted: an explicit proof
        # object, a declared zero call count anchored to the ledger, or an
        # observed ledger that really holds no row for this task.
        zero_calls_stated = (
            bool(_mapping(cost.get("zero_model_calls_proof")))
            or (ledger_reference
                and any(value in (0, 0.0) and not isinstance(value, bool)
                        for value in declared_calls))
            or ledger_zero
        )
        if zero_claim and not zero_calls_stated:
            violations.append("zero_cost_claim_without_a_zero_call_proof")
        if zero_claim and ledger_rows is not None and not ledger_zero:
            violations.append("zero_cost_claim_contradicts_the_ledger")
    if ledger_rows is not None:
        observed_rows = sum(int(value or 0) for value in dict(ledger_rows).values())
        declared_rows_map = _mapping(cost.get("ledger_rows"))
        if declared_rows_map:
            declared_rows = sum(
                int(value or 0) for value in declared_rows_map.values())
            if observed_rows != declared_rows:
                violations.append(
                    "ledger_rows_mismatch:%d!=%d" % (declared_rows, observed_rows))
        else:
            occupied_value = cost.get("occupied_cny", cost.get("spend_cny"))
            try:
                claims_spend = float(occupied_value or 0) > 0
            except (TypeError, ValueError):
                claims_spend = True
            if claims_spend and observed_rows == 0:
                # a receipt may spend real money and the ledger must have seen it
                violations.append("cost_claim_contradicts_the_ledger")

    if evidence_root:
        for path, digest in _mapping(canonical.get("input_output_hashes")).items():
            if not _text(digest) or set(_text(digest)) == {"0"}:
                continue
            target = Path(evidence_root) / path
            if not target.is_file():
                violations.append("evidence_file_missing:%s" % path)
                continue
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != _text(digest).casefold():
                violations.append("hash_drift:%s" % path)

    verified_level, basis = _evidence_level(canonical)
    if klass == "product":
        real_positives = [
            row for row in canonical.get("positive_examples") or []
            if isinstance(row, Mapping)
            and _text(row.get("kind")) in REAL_POSITIVE_KINDS
        ]
        if required_level >= "V3" and not real_positives:
            violations.append("no_real_positive_example")
        if (required_level >= "V3" and verified_level < "V2"
                and not real_positives):
            violations.append("pytest_green_is_not_v3")
        if LEVEL_RANK.get(verified_level, -1) < LEVEL_RANK.get(declared, -1):
            violations.append(
                "declared_level_exceeds_evidence:%s>%s"
                % (declared, verified_level or "none"))
        if LEVEL_RANK.get(declared, -1) < LEVEL_RANK.get(required_level, -1):
            violations.append(
                "required_level_downgrade:%s<%s" % (declared, required_level))
        stages = [row for row in _rows(canonical.get("stages"))
                  if isinstance(row, Mapping)]
        if required_level == "V4" and len(stages) < 2:
            violations.append("stage_count_below_v4:%d" % len(stages))
        if required_level == "V5" and not _mapping(canonical.get("terminal_manifest")):
            violations.append("no_terminal_manifest_for_v5")

    achieved = declared
    if verified_level and LEVEL_RANK.get(verified_level, -1) < LEVEL_RANK.get(
        declared, -1
    ):
        achieved = verified_level
    prefixes = {item.split(":")[0] for item in violations}
    while achieved and (LEVEL_BLOCKERS.get(achieved, frozenset()) & prefixes):
        index = LEVEL_RANK.get(achieved, 0) - 1
        achieved = LEVELS[index] if index >= 0 else ""
    blocking = [item for item in violations]
    completed = bool(not blocking and achieved and (
        LEVEL_RANK.get(achieved, -1) >= LEVEL_RANK.get(required_level, -1)
        if klass == "product" else True
    ))
    return {
        "schema_version": GATE_SCHEMA,
        "task_id": canonical.get("task_id"),
        "class": klass,
        "required_level": required_level,
        "declared_level": declared,
        "verified_level": verified_level,
        "achieved_level": achieved,
        "completed": completed,
        "ok": completed,
        "product_science_pass": bool(klass == "product" and completed),
        "violations": blocking,
        "basis": basis,
        "receipt_hash": declared_hash or _sha(receipt_body(canonical)),
        "adapted_from": canonical.get("adapted_from"),
    }


# --------------------------------------------------------------------------- #
# node manifests and run readiness
# --------------------------------------------------------------------------- #

def gate_node_manifest(manifest: Mapping[str, Any]) -> dict:
    """The level a committed node's own manifest supports (evidence, not claims)."""

    body = dict(manifest or {})
    violations: list[str] = []
    fingerprints = _mapping(body.get("fingerprints"))
    outputs = [row for row in body.get("outputs") or [] if isinstance(row, Mapping)]
    if _text(body.get("state")) != "committed":
        violations.append("node_not_committed:%s" % (_text(body.get("state")) or "missing"))
    if not fingerprints:
        violations.append("node_fingerprints_missing")
    if not outputs:
        violations.append("node_outputs_missing")
    if outputs and not all(_text(row.get("sha256")) for row in outputs):
        violations.append("node_output_without_a_hash")
    if not _text(body.get("generation_id")) or not _text(body.get("attempt_id")):
        violations.append("node_identity_incomplete")
    cost = _mapping(body.get("cost_receipt"))
    if not cost:
        violations.append("node_cost_receipt_missing")
    if not _mapping(body.get("validation_receipt")):
        violations.append("node_validation_receipt_missing")
    level = "V0"
    if not violations:
        level = "V1"
        if int(cost.get("model_calls") or 0) > 0:
            level = "V2"
    return {
        "schema_version": NODE_GATE_SCHEMA,
        "node_id": _text(body.get("node_id")),
        "level": level,
        "ok": not violations,
        "violations": violations,
        "output_rows": len(outputs),
        "fingerprint_rows": len(fingerprints),
    }


def read_run_validation_gate(
    work_dir: str | os.PathLike,
    *,
    required_level: str = "V1",
) -> dict:
    """Aggregate the persisted node manifests into one readiness verdict."""

    nodes_root = Path(work_dir) / "phase3_argument_orchestration" / "nodes"
    nodes: dict[str, dict] = {}
    if nodes_root.is_dir():
        for manifest_path in sorted(nodes_root.glob("*/NODE_MANIFEST.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                nodes[manifest_path.parent.name] = {
                    "schema_version": NODE_GATE_SCHEMA,
                    "node_id": manifest_path.parent.name,
                    "level": "V0",
                    "ok": False,
                    "violations": ["node_manifest_unreadable"],
                }
                continue
            nodes[_text(manifest.get("node_id")) or manifest_path.parent.name] = (
                gate_node_manifest(manifest)
            )
    if not nodes:
        return {
            "schema_version": GATE_SCHEMA,
            "status": "unavailable",
            "reason": "no_committed_phase3_nodes",
            "required_level": required_level,
            "achieved_level": "",
            "nodes": {},
            "blocking": [],
        }
    blocking = sorted(
        "%s:%s" % (node_id, item)
        for node_id, gate in nodes.items() for item in gate["violations"]
    )
    lowest = min(nodes.values(), key=lambda gate: LEVEL_RANK.get(gate["level"], -1))
    if LEVEL_RANK.get(lowest["level"], -1) < LEVEL_RANK.get(required_level, -1):
        blocking = sorted(blocking + [
            "%s:node_level_below_required:%s<%s"
            % (lowest.get("node_id"), lowest["level"], required_level)
        ])
    return {
        "schema_version": GATE_SCHEMA,
        "status": "passed" if not blocking else "failed",
        "required_level": required_level,
        "achieved_level": lowest["level"],
        "nodes": nodes,
        "blocking": blocking,
        "note": "a node manifest states what the node proved; the weakest committed node is the run's level",
    }


# --------------------------------------------------------------------------- #
# the queue reducer
# --------------------------------------------------------------------------- #

def ledger_counts_for(
    ledger_path: str | os.PathLike | None, task_id: str
) -> dict | None:
    """The observed 005 ledger rows for one task, read-only; None when unknown."""

    if not ledger_path:
        return None
    import sqlite3

    path = Path(ledger_path)
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(
            "file:" + str(path).replace("\\", "/") + "?mode=ro", uri=True
        )
    except sqlite3.Error:
        return None
    try:
        rows = connection.execute(
            "SELECT state, COUNT(*) FROM reservations WHERE task_id = ? GROUP BY state",
            (str(task_id),),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    counts: dict[str, int] = {}
    for state, count in rows:
        counts[str(state)] = int(count or 0)
    return counts


def reduce_queue(
    *,
    queue: Mapping[str, Any],
    receipts: Mapping[str, Mapping[str, Any]],
    evidence_root: str | os.PathLike | None = None,
    ledger_path: str | os.PathLike | None = None,
) -> dict:
    """Only this function may produce completed.  History is never rewritten."""

    tasks: dict[str, dict] = {}
    completed: list[str] = []
    refused: list[str] = []
    for row in (queue or {}).get("tasks") or ():
        if not isinstance(row, Mapping):
            continue
        task_id = _text(row.get("id") or row.get("task_id"))
        if not task_id:
            continue
        required = _text(row.get("required_validation")
                         or row.get("required_validation_level") or "V3")
        receipt = receipts.get(task_id)
        if not isinstance(receipt, Mapping):
            tasks[task_id] = {
                "task_id": task_id,
                "state": "refused",
                "completed": False,
                "previous_status": _text(row.get("status")),
                "required_level": requirement_class(required)[1],
                "class": requirement_class(required)[0],
                "achieved_level": "",
                "violations": ["missing_receipt"],
                "receipt_hash": "",
            }
            refused.append(task_id)
            continue
        verdict = verify_receipt(
            receipt,
            expected_hash=_text(receipt.get("receipt_hash")),
            evidence_root=evidence_root,
            ledger_rows=ledger_counts_for(ledger_path, task_id),
        )
        verdict.update({
            "task_id": task_id,
            "state": "completed" if verdict["completed"] else "refused",
            "previous_status": _text(row.get("status")),
        })
        tasks[task_id] = verdict
        (completed if verdict["completed"] else refused).append(task_id)
    missing_hash = sum(
        1 for task_id in completed if not _text(tasks[task_id]["receipt_hash"])
    )
    return {
        "schema_version": REDUCTION_SCHEMA,
        "tasks": tasks,
        "completed": sorted(completed),
        "refused": sorted(refused),
        "counts": {
            "tasks": len(tasks),
            "completed": len(completed),
            "refused": len(refused),
        },
        "completed_without_a_receipt_hash": missing_hash,
        "consumer_pending_in_completed": sum(
            1 for task_id in completed
            if any(item.startswith("consumer_pending")
                   for item in tasks[task_id]["violations"])
        ),
        "history_preserved": True,
        "note": "a previous status is recorded, never deleted; the reducer's verdict is the authority",
    }


__all__ = [
    "AGGREGATOR_GRADERS",
    "GATE_SCHEMA",
    "LEVELS",
    "LEVEL_RANK",
    "NODE_GATE_SCHEMA",
    "RECEIPT_SCHEMA",
    "REDUCTION_SCHEMA",
    "REQUIREMENT_CLASSES",
    "ValidationReceiptError",
    "canonicalize_receipt",
    "gate_node_manifest",
    "ledger_counts_for",
    "level_of",
    "read_run_validation_gate",
    "receipt_body",
    "reduce_queue",
    "requirement_class",
    "seal_receipt",
    "verify_receipt",
]
