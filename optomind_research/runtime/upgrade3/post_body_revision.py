"""Isolated A/B/C post-BODY revision experiments; never imports a paid client.

The caller injects a synchronous client(stage_id, messages, model=...). All
inputs, attempts and decisions are retained. Resume replays durable attempts,
including failures, rather than silently repeating a potentially paid call.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping

from .post_body_revision_contracts import (
    RevisionContractError, apply_patches, evidence_for_issue, normalize_case,
    resolve_target, validate_patch,
)

VERSION = "post-body-revision.v1"
PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts" / "post_body_revision"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes((json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    os.replace(temp, path)


def _positive_int(value: Any, name: str, *, zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if zero else 1):
        raise RevisionContractError(f"invalid_{name}")
    return value


def _config(config: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(config)
    result.setdefault("variant", "B")
    if result["variant"] not in {"A", "B", "C"}:
        raise RevisionContractError("variant_must_be_A_B_or_C")
    result["max_issues"] = _positive_int(result.get("max_issues", 6), "max_issues", zero=True)
    result["max_input_chars"] = _positive_int(result.get("max_input_chars", 200000), "max_input_chars")
    result["max_target_chars"] = _positive_int(result.get("max_target_chars", 12000), "max_target_chars")
    result["max_material_index_chars"] = _positive_int(result.get("max_material_index_chars", 24000), "max_material_index_chars")
    if not isinstance(result.get("models"), Mapping):
        raise RevisionContractError("models_mapping_required")
    roles = {"review", "verifier"} | ({"author"} if result["variant"] != "A" else set())
    if result["variant"] == "C":
        roles.add("escalation")
    for role in roles:
        if not isinstance(result["models"].get(role), str) or not result["models"][role].strip():
            raise RevisionContractError(f"model_required:{role}")
    return result


def _parse_response(raw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    usage: dict[str, Any] = {}
    body = raw
    if isinstance(raw, Mapping):
        usage = dict(raw.get("usage") or {}) if isinstance(raw.get("usage"), Mapping) else {}
        if raw.get("complete") is False:
            raise ValueError("incomplete_response:complete_false")
        finish = raw.get("finish_reason")
        if "choices" in raw:
            choices = raw["choices"]
            if not isinstance(choices, list) or not choices:
                raise ValueError("empty_choices")
            finish = choices[0].get("finish_reason", finish)
            body = choices[0].get("message", {}).get("content")
        elif "content" in raw:
            body = raw["content"]
        elif "response" in raw:
            body = raw["response"]
        if finish not in (None, "stop", "completed"):
            raise ValueError(f"incomplete_response:{finish}")
    if isinstance(body, str):
        if not body.strip():
            raise ValueError("empty_response")
        body = json.loads(body)
    if not isinstance(body, Mapping) or not body:
        raise ValueError("response_not_nonempty_object")
    return dict(body), usage


class _Calls:
    def __init__(self, out: Path, config: dict, prompts: dict, fingerprint: str, client: Callable):
        self.out, self.config, self.prompts = out, config, prompts
        self.fingerprint, self.client = fingerprint, client
        self.records: list[dict] = []

    def call(self, stage: str, prompt: str, payload: dict, role: str) -> dict:
        messages = [{"role": "system", "content": self.prompts[prompt]},
                    {"role": "user", "content": _json(payload)}]
        model = self.config["models"][role]
        request = {"stage_id": stage, "model": model, "messages": messages}
        digest = _hash({"run": self.fingerprint, "request": request})
        path = self.out / "calls" / f"{stage}.json"
        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("fingerprint") != digest:
                raise RevisionContractError(f"call_cache_contract_changed:{stage}")
            if record.get("status") == "started":
                record.update(status="error", error="interrupted_call_outcome_unknown; no automatic retry")
                _write(path, record)
        else:
            record = {**request, "fingerprint": digest, "status": "started", "usage": {}}
            _write(path, record)
            if sum(len(m["content"]) for m in messages) > self.config["max_input_chars"]:
                record.update(status="error", error="max_input_chars_exceeded; input not truncated", dispatched=False)
            else:
                try:
                    record["dispatched"] = True
                    _write(path, record)  # persist dispatch intent before an ambiguous crash
                    started = time.monotonic()
                    try:
                        raw = self.client(stage, messages, model=model)
                    finally:
                        record["latency_seconds"] = time.monotonic() - started
                    if hasattr(raw, "model_dump"):
                        raw = raw.model_dump()
                    # Keep the raw response before attempting any schema parsing.
                    record["raw_response"] = raw
                    if isinstance(raw, Mapping):
                        cost = raw.get("cost_cny")
                        record["cost_cny"] = cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0 else None
                        record["cost_provenance"] = raw.get("cost_provenance", "not_reported")
                    if isinstance(raw, Mapping) and isinstance(raw.get("usage"), Mapping):
                        record["usage"] = dict(raw["usage"])
                    _write(path, record)
                    parsed, usage = _parse_response(raw)
                    record.update(status="ok", response=parsed, usage=usage)
                except Exception as exc:
                    record.update(status="error", error=f"{type(exc).__name__}: {exc}")
            _write(path, record)
        self.records.append(record)
        if record["status"] != "ok":
            raise RevisionContractError(f"call_failed:{stage}:{record.get('error', 'unknown')}")
        return record["response"]


def _issue_record(issue: Any, position: int) -> dict:
    return {"issue": issue, "position": position, "proposed": False, "verified": False,
            "applied": False, "pending": True, "dismissed": False, "status": "pending",
            "reason": "not_processed", "attempts": []}


def _validate_issue(issue: Any, seen: set[str]) -> None:
    if not isinstance(issue, Mapping):
        raise RevisionContractError("issue_not_object")
    identity = issue.get("issue_id")
    if not isinstance(identity, str) or not identity.strip() or identity in seen:
        raise RevisionContractError("invalid_or_duplicate_issue_id")
    seen.add(identity)
    if issue.get("kind") not in {"editorial", "scientific", "missing"}:
        raise RevisionContractError("unknown_issue_kind")
    if "preserve" not in issue or not isinstance(issue["preserve"], list):
        raise RevisionContractError("issue_preserve_list_required")
    if not isinstance(issue.get("problem"), str) or not issue["problem"].strip():
        raise RevisionContractError("issue_problem_required")
    if issue.get("priority", "medium") not in {"high", "medium", "low"}:
        raise RevisionContractError("invalid_issue_priority")
    if issue.get("operation", "replace") not in {"replace", "remove", "insert_after"}:
        raise RevisionContractError("unknown_operation")


def _selected_source_identities(case: dict, evidence: Mapping[str, Any]) -> dict:
    """Project only linked identities, retaining conflicting declarations.

    An exact handle-to-material link selects metadata; it does not establish
    paper identity or content entailment. Local paths/history are not exposed.
    """
    fields = {"paper_id", "doi", "DOI", "title", "year", "pmid", "pmcid",
              "arxiv_id", "semantic_scholar_id", "source_handle"}
    linked: dict[str, set[str]] = {}
    for material_id, material in evidence.items():
        if material_id in case["source_identity_map"]:
            linked.setdefault(material_id, set()).add(material_id)
        for handle in material.get("source_handles", []):
            linked.setdefault(handle, set()).add(material_id)
    for handle, identity in case["source_identity_map"].items():
        ids = ([identity] if isinstance(identity, str) else
               identity.get("material_ids", [identity.get("material_id")]) if isinstance(identity, Mapping) else [])
        if isinstance(ids, list):
            selected = {item for item in ids if isinstance(item, str) and item in evidence}
            if selected:
                linked.setdefault(handle, set()).update(selected)
    result = {}
    for handle in sorted(linked):
        identity = case["source_identity_map"].get(handle)
        row: dict[str, Any] = {"material_ids": sorted(linked[handle])}
        if isinstance(identity, Mapping):
            declared = {key: identity[key] for key in sorted(fields) if key in identity}
            original = identity.get("original_identity")
            if declared:
                row["declared_identity"] = declared
            if isinstance(original, Mapping):
                row["original_identity"] = {key: original[key] for key in sorted(fields) if key in original}
            row["identity_available"] = bool(declared or row.get("original_identity"))
        else:
            row["identity_available"] = False
        result[handle] = row
    return result


def _context(case: dict, issue: dict, target: dict, evidence: dict) -> dict:
    blocks = case["blocks"]
    related_ids = issue.get("related_block_ids", [])
    if not isinstance(related_ids, list) or any(not isinstance(identity, str) for identity in related_ids):
        raise RevisionContractError("related_block_ids_list_required")
    if len(related_ids) != len(set(related_ids)):
        raise RevisionContractError("duplicate_related_block_id")
    block_map = {block["block_id"]: block for block in blocks}
    if any(identity not in block_map for identity in related_ids):
        raise RevisionContractError("unknown_related_block_id")
    index = next(i for i, b in enumerate(blocks) if b["block_id"] == target["block_id"])
    return {"issue": issue, "target": target,
            "target_block": blocks[index], "related_blocks": [block_map[identity] for identity in related_ids], "neighbors": blocks[max(0, index-1):index] + blocks[index+1:index+2],
            "research_scope": {"research_question": case["research_question"], "scope": case["scope"]},
            "outline_intent": case["outline"], "chapter_intent": case.get("chapter_intent", ""),
            "evidence": evidence, "selected_source_identities": _selected_source_identities(case, evidence)}


def _check_context_dependencies(issue: Mapping[str, Any], records: list[dict]) -> None:
    """Do not combine edits whose reviewed counterpart context would change.

    Every verifier reads the immutable snapshot. Joint multi-block semantic
    transactions are deliberately unsupported; shared unchanged context is fine.
    """
    related = set(issue.get("related_block_ids", []))
    target = issue["target_block_id"]
    for record in records:
        if not record.get("applied"):
            continue
        prior = record["issue"]
        if target in prior.get("related_block_ids", []) or prior["target_block_id"] in related:
            raise RevisionContractError(f"context_dependency:{prior['issue_id']}; joint multi-block edits require separate review")


def _ordered_issues(issues: list) -> list[tuple[int, Any]]:
    priority_rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(enumerate(issues, 1), key=lambda item: (
        priority_rank.get(item[1].get("priority", "medium"), 1) if isinstance(item[1], Mapping) else 1, item[0]))


def _review_payload(case: dict, settings: dict) -> dict:
    material_index = [{"material_id": identity, "title": m.get("title", ""),
                       "summary": m.get("summary", ""), "source_handles": m.get("source_handles", []), "available_fields": sorted(m),
                       "text_chars": len(m["text"])} for identity, m in case["materials"].items()]
    payload = {"research_scope": {"research_question": case["research_question"], "scope": case["scope"]},
               "outline_intent": case["outline"], "actual_snapshot_blocks": case["blocks"],
               "material_index": material_index, "max_issues": settings["max_issues"]}
    known = settings.get("known_fixed_issues", case.get("known_fixed_issues"))
    if known is not None:
        payload["known_fixed_issues"] = known
    return payload


def preflight_revision(case: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """Return exact initial payload sizes without calls/files; raise on overflow.

    Dynamic author/verifier prompts remain checked immediately before dispatch.
    Fixed-issue author payloads can additionally be checked before any spending.
    No input is truncated to fit a configured limit.
    """
    normalized, settings = normalize_case(case), _config(config)
    payload = _review_payload(normalized, settings)
    stage = "combined" if settings["variant"] == "A" else "review"
    prompt = (PROMPTS_DIR / f"{stage}.md").read_text(encoding="utf-8")
    review_chars = len(prompt) + len(_json(payload))
    index_chars = len(_json(payload["material_index"]))
    known = settings.get("known_fixed_issues", normalized.get("known_fixed_issues"))
    checks = {"variant": settings["variant"], "base_sha256": normalized["base_sha256"],
              "review_input_chars": review_chars, "material_index_chars": index_chars,
              "max_input_chars": settings["max_input_chars"],
              "max_material_index_chars": settings["max_material_index_chars"],
              "max_target_chars": settings["max_target_chars"],
              "review_call_required": known is None or settings["variant"] == "A",
              "fixed_issue_author_input_chars": [], "dynamic_inputs_checked_before_dispatch": True}
    if index_chars > settings["max_material_index_chars"]:
        raise RevisionContractError(f"max_material_index_chars_exceeded:{index_chars}>{settings['max_material_index_chars']}; input not truncated")
    if checks["review_call_required"] and review_chars > settings["max_input_chars"]:
        raise RevisionContractError(f"max_input_chars_exceeded:{review_chars}>{settings['max_input_chars']}; input not truncated")
    if known is not None:
        if not isinstance(known, list) or any(not isinstance(i, Mapping) for i in known):
            raise RevisionContractError("known_fixed_issues_list_required")
        for _, issue in _ordered_issues(known)[:settings["max_issues"]]:
            target = resolve_target(normalized, issue)
            if len(target["original_text"]) > settings["max_target_chars"]:
                raise RevisionContractError("max_target_chars_exceeded; explicit narrower scope or config required")
            context = _context(normalized, issue, target, evidence_for_issue(normalized, issue))
            chars = len((PROMPTS_DIR / "author.md").read_text(encoding="utf-8")) + len(_json(context))
            checks["fixed_issue_author_input_chars"].append({"issue_id": issue.get("issue_id"), "chars": chars})
            if settings["variant"] != "A" and chars > settings["max_input_chars"]:
                raise RevisionContractError(f"max_input_chars_exceeded:author:{chars}>{settings['max_input_chars']}; input not truncated")
    return checks


def _attempt(calls: _Calls, case: dict, issue: dict, context: dict, prefix: str,
             proposal: Any = None, *, escalation: bool = False, previous: dict | None = None) -> dict:
    attempt: dict[str, Any] = {"escalation": escalation, "status": "pending"}
    try:
        if proposal is None:
            payload = dict(context)
            if previous is not None:
                payload["previous_attempt"] = previous
            proposal = calls.call(prefix + "_author", "author", payload, "escalation" if escalation else "author")
        if not isinstance(proposal, Mapping):
            raise RevisionContractError("proposal_not_object")
        attempt["proposal"] = dict(proposal)
        if proposal.get("issue_id", issue["issue_id"]) != issue["issue_id"]:
            raise RevisionContractError("proposal_issue_id_mismatch")
        cited_proposal = proposal.get("evidence_ids", [])
        if not isinstance(cited_proposal, list) or any(not isinstance(x, str) or x not in context["evidence"] for x in cited_proposal):
            raise RevisionContractError("proposal_unknown_evidence_id")
        no_change = proposal.get("no_change") is True
        patch = None
        if no_change:
            if not isinstance(proposal.get("reason"), str) or not proposal["reason"].strip():
                raise RevisionContractError("no_change_requires_reason")
        else:
            patch = validate_patch(case, issue, proposal)
        verdict = calls.call(prefix + "_verifier", "verifier",
                             {**context, "original": issue["original_text"], "candidate": proposal},
                             "escalation" if escalation else "verifier")
        attempt["verification"] = verdict
        if verdict.get("verdict") not in {"accept", "reject", "insufficient_evidence", "issue_not_supported"}:
            raise RevisionContractError("invalid_verifier_verdict")
        if type(verdict.get("preservation_ok")) is not bool or type(verdict.get("problem_improved")) is not bool:
            raise RevisionContractError("verifier_booleans_required")
        if not isinstance(verdict.get("reason"), str) or not verdict["reason"].strip():
            raise RevisionContractError("verifier_reason_required")
        cited = verdict.get("evidence_ids", [])
        if not isinstance(cited, list) or any(not isinstance(x, str) or x not in context["evidence"] for x in cited):
            raise RevisionContractError("verifier_unknown_evidence_id")
        if verdict["verdict"] == "accept" and issue["kind"] in {"scientific", "missing"} and not cited:
            raise RevisionContractError("scientific_accept_requires_evidence_ids")
        attempt["verified"] = True
        attempt["reason"] = verdict["reason"]
        if verdict["verdict"] == "issue_not_supported":
            attempt["status"] = "dismissed"
        elif verdict["verdict"] == "accept" and verdict["preservation_ok"] is True and verdict["problem_improved"] is True and not no_change:
            attempt.update(status="accepted", patch=patch)
        elif verdict["verdict"] == "insufficient_evidence":
            attempt["status"] = "insufficient_evidence"
        else:
            attempt["status"] = "no_change" if no_change else "rejected"
    except Exception as exc:
        attempt.update(status="error", reason=f"{type(exc).__name__}: {exc}")
    return attempt


def run_revision(case: Mapping[str, Any], config: Mapping[str, Any], output_dir: str | Path,
                 client: Callable, resume: bool = False) -> dict[str, Any]:
    """Run one independent alternative against an immutable common snapshot.

    Existing output directories require exact-contract ``resume=True``. Schema,
    prompt, model or input changes require a fresh output directory. No automatic
    retries are performed, and neither the baseline nor legacy runtime is edited.
    """
    normalized, settings = normalize_case(case), _config(config)
    prompts = {name: (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
               for name in ("review", "combined", "author", "verifier")}
    contract = {"version": VERSION, "case": normalized, "config": settings, "prompts": prompts,
                "implementation_hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in (Path(__file__), Path(__file__).with_name("post_body_revision_contracts.py"))}}
    fingerprint = _hash(contract)
    out = Path(output_dir).resolve()
    manifest = out / "manifest.json"
    if manifest.exists():
        old = json.loads(manifest.read_text(encoding="utf-8"))
        if old.get("fingerprint") != fingerprint:
            raise RevisionContractError("resume_contract_changed; use a fresh output directory")
        if not resume:
            raise RevisionContractError("output_already_exists; explicit resume required")
        if (out / "report.json").exists():
            saved = json.loads((out / "report.json").read_text(encoding="utf-8"))
            if saved.get("fingerprint") != fingerprint:
                raise RevisionContractError("report_contract_changed")
            for name, digest in (("baseline.md", normalized["base_sha256"]), ("candidate.md", saved.get("candidate_sha256"))):
                if not (out / name).exists() or hashlib.sha256((out / name).read_bytes()).hexdigest() != digest:
                    raise RevisionContractError(f"resume_artifact_changed:{name}")
            return saved
    elif out.exists() and any(out.iterdir()):
        raise RevisionContractError("nonempty_output_without_manifest")
    if not callable(client):
        raise RevisionContractError("callable_client_required; preview must not run the engine")
    out.mkdir(parents=True, exist_ok=True)
    lock = out / ".run.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RevisionContractError("output_locked_by_another_run; inspect before removing stale lock") from exc
    os.close(descriptor)
    try:
        _write(manifest, {"fingerprint": fingerprint, **contract})
        _write(out / "input_case.json", normalized)
        _write(out / "config.json", settings)
        baseline = out / "baseline.md"
        candidate = out / "candidate.md"
        if baseline.exists() and baseline.read_bytes() != normalized["draft_text"].encode("utf-8"):
            raise RevisionContractError("resume_artifact_changed:baseline.md")
        baseline.write_bytes(normalized["draft_text"].encode("utf-8"))
        calls = _Calls(out, settings, prompts, fingerprint, client)
        report: dict[str, Any] = {"schema_version": VERSION, "fingerprint": fingerprint,
            "case_id": normalized["case_id"], "base_sha256": normalized["base_sha256"],
            "variant": settings["variant"], "status": "completed", "run_completed": True,
            "model_assessed_issues_resolved": False, "publication_approved": False,
            "case_fingerprint": _hash(normalized),
            "evidence_fingerprint": _hash({"materials": normalized["materials"], "source_identity_map": normalized["source_identity_map"]}),
            "cost_provenance": "client_reported_usage_only; no inferred paid spend",
            "execution_mode": getattr(client, "execution_mode", settings.get("execution_mode", "injected_client")),
            "issue_records": [], "applied_patches": [], "errors": [],
            "baseline_path": str(baseline), "candidate_path": str(candidate), "output_dir": str(out)}
        review_payload = _review_payload(normalized, settings)
        material_index = review_payload["material_index"]
        known = settings.get("known_fixed_issues", normalized.get("known_fixed_issues"))
        try:
            if len(_json(material_index)) > settings["max_material_index_chars"]:
                raise RevisionContractError("max_material_index_chars_exceeded; input not truncated")
            stage = "combined" if settings["variant"] == "A" else "review"
            if known is not None and (not isinstance(known, list) or any(not isinstance(i, Mapping) for i in known)):
                raise RevisionContractError("known_fixed_issues_list_required")
            if known is not None and settings["variant"] in {"B", "C"}:
                review = {"issues": known}
                report["review_origin"] = "fixed_issue_experiment"
            else:
                review = calls.call(stage, stage, review_payload, "review")
                report["review_origin"] = "combined_fixed_issue_experiment" if known is not None else "model_review"
                if known is not None:
                    got = review.get("issues")
                    if not isinstance(got, list) or [i.get("issue_id") for i in got if isinstance(i, Mapping)] != [i.get("issue_id") for i in known]:
                        raise RevisionContractError("combined_fixed_issue_identity_mismatch")
                    # Fixed issues are the authoritative experiment contract.
                    review["issues"] = known
            if not isinstance(review.get("issues"), list):
                raise RevisionContractError("review_issues_list_required")
            proposals = review.get("proposals", [])
            if not isinstance(proposals, list):
                raise RevisionContractError("review_proposals_list_required")
            proposal_map: dict[str, dict] = {}
            for p in proposals:
                if not isinstance(p, Mapping) or not isinstance(p.get("issue_id"), str) or p["issue_id"] in proposal_map:
                    raise RevisionContractError("invalid_or_duplicate_proposal")
                proposal_map[p["issue_id"]] = dict(p)
            seen: set[str] = set()
            ordered_issues = _ordered_issues(review["issues"])
            selected_positions = {position for position, _ in ordered_issues[:settings["max_issues"]]}
            for position, issue in ordered_issues:
                record = _issue_record(issue, position)
                report["issue_records"].append(record)
                try:
                    _validate_issue(issue, seen)
                    if position not in selected_positions:
                        record["reason"] = "issue_processing_budget_exhausted"
                        continue
                    target = resolve_target(normalized, issue)
                    if len(target["original_text"]) > settings["max_target_chars"]:
                        raise RevisionContractError("max_target_chars_exceeded; explicit narrower scope or config required")
                    evidence = evidence_for_issue(normalized, issue)
                    context = _context(normalized, issue, target, evidence)
                    _check_context_dependencies(issue, report["issue_records"])
                    prefix = f"issue_{position:03d}"
                    if settings["variant"] == "A" and issue["issue_id"] not in proposal_map:
                        record["reason"] = "combined_review_did_not_propose_patch"
                        continue
                    initial = _attempt(calls, normalized, issue, context, prefix,
                                       proposal_map.get(issue["issue_id"]) if settings["variant"] == "A" else None)
                    record["attempts"].append(initial)
                    last = initial
                    # Escalate only one supported substantive issue attempt. An
                    # invalid anchor, missing evidence or failed call is not a
                    # reason to spend another call, and review is never repeated.
                    if (settings["variant"] == "C" and issue["kind"] in {"scientific", "missing"}
                            and evidence and initial["status"] in {"rejected", "no_change"}):
                        last = _attempt(calls, normalized, issue, context, prefix + "_escalation",
                                        escalation=True, previous=initial)
                        record["attempts"].append(last)
                    record["proposed"] = any("proposal" in a and a["proposal"].get("no_change") is not True for a in record["attempts"])
                    record["verified"] = any(a.get("verified") is True for a in record["attempts"])
                    record["reason"] = last.get("reason", last["status"])
                    if last["status"] == "dismissed":
                        record.update(status="dismissed", dismissed=True, pending=False)
                    elif last["status"] == "accepted":
                        _check_context_dependencies(issue, report["issue_records"])
                        patches = report["applied_patches"] + [last["patch"]]
                        apply_patches(normalized, patches)  # fail before admitting overlap
                        report["applied_patches"] = patches
                        record.update(status="applied", applied=True, pending=False)
                    else:
                        record["status"] = "pending"
                except Exception as exc:
                    record["reason"] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            report.update(status="failed", run_completed=False)
            report["errors"].append(f"{type(exc).__name__}: {exc}")
        result = apply_patches(normalized, report["applied_patches"])
        candidate.write_bytes(result["draft_text"].encode("utf-8"))
        report["candidate_sha256"] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        report["issue_records"].sort(key=lambda record: record["position"])
        records = report["issue_records"]
        report["counts"] = {name: sum(r[name] is True for r in records)
                            for name in ("proposed", "verified", "applied", "pending", "dismissed")}
        report["counts"]["reviewed"] = len(records)
        report["model_assessed_issues_resolved"] = report["run_completed"] and bool(records) and not any(
            r["pending"] for r in records)
        report["verification_status"] = "model_assessed_issues_resolved" if report["model_assessed_issues_resolved"] else ("no_issues_reported" if report["run_completed"] and not records else "unresolved_or_incomplete")
        report["generation_mode"] = report["execution_mode"]
        report["program_guards"] = {"snapshot_bound": True, "exact_anchors": True, "known_evidence_only": True, "accepted_patches_revalidated": True, "semantic_judgment": "independent_model_verifier; not scientific proof"}
        report["call_records"] = [{k: v for k, v in r.items() if k not in {"messages", "raw_response", "response"}}
                                  for r in calls.records]
        totals: Counter = Counter()
        for call in calls.records:
            for key, value in call.get("usage", {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[key] += value
        report["usage"] = dict(totals)
        report["latency_seconds"] = sum(c.get("latency_seconds", 0) for c in calls.records)
        dispatched = [c for c in calls.records if c.get("dispatched") is True]
        costs = [c.get("cost_cny") for c in dispatched]
        report["cost_cny"] = sum(costs) if costs and all(c is not None for c in costs) else None
        report["known_cost_cny"] = sum(c for c in costs if c is not None)
        report["cost_complete"] = bool(costs) and all(c is not None for c in costs)
        report["cost_provenance"] = sorted({c.get("cost_provenance", "not_reported") for c in dispatched}) or ["no_dispatched_calls"]
        report["calls"] = len(calls.records)
        _write(out / "report.json", report)
        return report
    finally:
        lock.unlink(missing_ok=True)
