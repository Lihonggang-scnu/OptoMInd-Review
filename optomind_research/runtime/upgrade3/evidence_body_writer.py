"""Additive complete-BODY routes over lossless, separately archived evidence.

All model calls use the existing common transport, cache and shared ledger.
Chapter composition and bounded missing-task insertions are explicit stages.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Mapping, Sequence
import uuid

from . import writing_evidence
from .fullbody_contracts import fullbody_task_catalog, parse_fullbody_response, _transport_status
from .fullbody_writer import (_cost_summary, _dependency, _execute_stage, _join,
    _object, _reject_outcome, _stage, _text_hash, _transport_complete,
    _source_file_hashes as _old_source_file_hashes)
from .writer_candidates import ROOT, _hash, _output_lock, _profile, _read, _safe_error, _write
from .writer_candidates_contracts import CandidateError

SCHEMA_VERSION = "optomind.evidence_body_writer.v1"
ROUTES = ("packed_whole", "packed_continuous", "dossier_author")
PROMPT_ROOT = ROOT / "prompts" / "evidence_body_writer"


def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        raise CandidateError("config_must_be_object")
    allowed = {"schema_version", "purpose", "writer", "curator", "reader", "reviser",
               "max_input_tokens", "max_rereads_per_window", "max_author_calls", "completion_on_missing"}
    if set(config) - allowed:
        raise CandidateError("unknown_config_keys:" + ",".join(sorted(set(config) - allowed)))
    result = deepcopy(dict(config))
    for role in ("writer", "curator", "reader", "reviser"):
        raw = config.get(role, {})
        if not isinstance(raw, Mapping):
            raise CandidateError("profile_must_be_object:" + role)
        result[role] = _profile({"thinking_budget": 16384, "max_output_tokens": 49152,
            "timeout_seconds": 1800, "stream_overall_timeout_seconds": 3600, **raw}, role)
    for key, default in (("max_rereads_per_window", 3), ("max_author_calls", 64), ("max_input_tokens", None)):
        value = config.get(key, default)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise CandidateError(key + "_must_be_positive_integer")
            result[key] = value
    result["completion_on_missing"] = config.get("completion_on_missing", True)
    if not isinstance(result["completion_on_missing"], bool):
        raise CandidateError("completion_on_missing_must_be_boolean")
    return result


def _source_file_hashes():
    result = _old_source_file_hashes()
    for path in (Path(__file__), Path(writing_evidence.__file__), Path(__file__).with_name("fullbody_writer.py")):
        result[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _prompt(*names):
    return "\n\n".join((PROMPT_ROOT / (name + ".md")).read_text(encoding="utf-8") for name in names)


def _messages(prompt, payload):
    return [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}]


def _scope_atoms(pack, ids, selections=None):
    if selections is None:
        return None
    return list(dict.fromkeys(aid for row in selections if set(ids).intersection(row["task_ids"]) for aid in row["selected_atom_ids"]))


def _payload(pack, ids, segments, selections=None, reread_atoms=()):
    selected = _scope_atoms(pack, ids, selections)
    if reread_atoms:
        initial = selected if selected is not None else [atom["atom_id"] for atom in writing_evidence.evidence_payload(pack, ids)["evidence_atoms"]]
        selected = list(dict.fromkeys([*initial, *reread_atoms]))
    payload = writing_evidence.evidence_payload(pack, ids, selected)
    payload.update(accepted_body_markdown=_join(segments), accepted_prose_scope="entire_actual_prefix",
        global_chapter_roles=payload.pop("chapter_roles", []),
        original_requirements_immutable=True, preceding_prose_is_scientific_evidence=False,
        provisional_prefix_segments=[{"segment_id": row["segment_id"], "pending_task_ids": row.get("pending_task_ids", [])}
            for row in segments if not row.get("accepted", True)],
        writing_position={"completed_task_ids": [key for segment in segments for key in segment.get("completed_task_ids", segment["task_ids"])],
            "unresolved_prior_task_ids": [key for row in segments for key in row.get("pending_task_ids", [])],
            "current_task_ids": list(ids), "remaining_task_ids": [key for key in pack["tasks"]
                if key not in ids and not any(key in row["task_ids"] for row in segments)]},
        reread_source_navigation=[{"source_handle": handle, "title": identity.get("title", ""),
            "atom_count": len(pack["source_atom_ids"][handle])} for handle, identity in pack["source_identities"].items()],
        full_pool_reread_available=True)
    return payload


def _author_stage(pack, ids, route, segments, selections, config, counter, index, reads=(), round_no=0):
    payload = _payload(pack, ids, segments, selections, reads)
    return _stage(f"author_{index:03d}" + (f"_reread_{round_no:02d}" if round_no else ""), "writer",
        _messages(_prompt("writer", route), payload), config["writer"], config, counter,
        task_ids=list(ids), selected_atom_ids=[atom["atom_id"] for atom in payload["evidence_atoms"]],
        full_prefix_sha256=_text_hash(payload["accepted_body_markdown"]),
        window_reason="complete_body" if len(ids) == len(pack["tasks"]) else "natural_chapter_or_capacity_only_intact_task_split")


def _fit_scope(candidates, build):
    """Split only when actual complete scope fails capacity, never by quota."""
    whole = build(candidates)
    if whole["estimate"]["fits"] or len(candidates) == 1:
        return whole
    best = None
    for size in range(1, len(candidates)):
        trial = build(candidates[:size])
        if not trial["estimate"]["fits"]:
            break
        best = trial
    return best or build(candidates[:1])


def _author_parser(book, ids):
    def parse(response):
        try:
            obj = _object(response)
        except (ValueError, TypeError):
            obj = {}
        if "read_atom_ids" in obj or "read_source_handles" in obj:
            atoms, sources = obj.get("read_atom_ids", []), obj.get("read_source_handles", [])
            if any(not isinstance(v, list) for v in (atoms, sources)) or not (atoms or sources) or obj.get("body_markdown"):
                raise CandidateError("invalid_evidence_reread")
            for values in (atoms, sources):
                if any(not isinstance(v, str) or not v for v in values) or len(set(values)) != len(values):
                    raise CandidateError("invalid_evidence_reread_ids")
            return {"kind": "reread_request", "read_atom_ids": atoms, "read_source_handles": sources,
                    "complete": _transport_complete(response)}
        return {**parse_fullbody_response(response, book, ids), "kind": "author"}
    return parse


def _curator_parser(pack, ids, considered_atom_ids=None):
    def parse(response):
        obj = _object(response)
        chosen = obj.get("selected_atom_ids")
        if not isinstance(chosen, list):
            raise CandidateError("curator_requires_atom_selection_list")
        if considered_atom_ids is not None and any(aid not in considered_atom_ids for aid in chosen):
            raise CandidateError("curator_atom_outside_considered_batch")
        packet = writing_evidence.select_atoms(pack, ids, chosen)
        return {"kind": "selection", "task_ids": list(ids), "selected_atom_ids": [atom["atom_id"] for atom in packet["evidence_atoms"]],
            "selection_reason": obj.get("selection_reason", ""),
            "complete": obj.get("complete") is True and _transport_complete(response)}
    return parse


def _completion_parser(book, ids, original, previous, missing):
    def parse(response):
        obj = _object(response)
        insertions = obj.get("insertions")
        claimed = obj.get("completed_task_ids", [])
        if not isinstance(insertions, list) or not insertions or not isinstance(claimed, list) or any(x not in missing for x in claimed):
            raise CandidateError("invalid_missing_task_completion")
        points = []
        for insertion in insertions:
            if not isinstance(insertion, Mapping):
                raise CandidateError("completion_insertion_not_object")
            anchor, text = insertion.get("after_anchor", ""), insertion.get("text")
            if not isinstance(anchor, str) or not isinstance(text, str) or not text.strip():
                raise CandidateError("invalid_completion_insertion")
            if anchor and original.count(anchor) != 1:
                raise CandidateError("completion_anchor_must_match_once")
            points.append((original.index(anchor) + len(anchor) if anchor else len(original), text))
        if len({point for point, _ in points}) != len(points):
            raise CandidateError("completion_duplicate_insertion_point")
        merged = original
        for point, text in sorted(points, reverse=True):
            merged = merged[:point] + text + merged[point:]
        completed = list(dict.fromkeys([*previous.get("completed_task_ids", []), *claimed]))
        parsed = parse_fullbody_response({"body_markdown": merged, "completed_task_ids": completed,
            "complete": obj.get("complete") is True and _transport_complete(response)}, book, ids)
        return {**parsed, "kind": "completion", "insertions": deepcopy(insertions),
            "original_body_sha256": _text_hash(original), "untouched_prose_preserved": True}
    return parse


def _normal_stage_transport(outcome):
    """Semantic parse errors can retain prose; uncertain transport cannot continue."""
    result = outcome.get("result", {})
    if not result.get("transport_complete") or result.get("call_error") or outcome.get("call_error"):
        return False
    path = Path(outcome["attempt_dir"]) / "RAW_RESPONSE.json" if outcome.get("attempt_dir") else None
    if path is None or not path.exists():
        return False
    raw = _read(path)
    return _transport_complete(raw) and not _transport_status(raw)[1]


def run_evidence_body(book: dict[str, Any], *, route: str, output_dir: str | Path,
                      config: Mapping[str, Any], client_factory=None, run=False,
                      retry_failed=False, counter=None, token_counter=None):
    """Preview or run one additive route; no hidden network or provider calls."""
    if route not in ROUTES:
        raise CandidateError("unknown_evidence_body_route:" + str(route))
    config = validate_config(config)
    with _output_lock(Path(output_dir).resolve()):
        return _run(book, route, Path(output_dir).resolve(), config, client_factory, run,
                    retry_failed, counter if counter is not None else token_counter)


def _run(book, route, output, config, factory, run, retry_failed, counter):
    pack = writing_evidence.compile_evidence(book)
    catalog = fullbody_task_catalog(book)
    if not catalog:
        raise CandidateError("fullbody_has_no_tasks")
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:10]
    run_dir = output / "runs" / run_id
    input_hash = _hash(book)
    input_dir = output / "inputs" / input_hash
    source_hashes = _source_file_hashes()
    prompt_hashes = {p.name: _text_hash(p.read_text(encoding="utf-8")) for p in PROMPT_ROOT.glob("*.md")}
    code_hash = _hash(source_hashes)
    for name, value in (("FULL_BODY_INPUT.json", book), ("WRITING_EVIDENCE.json", pack),
                        ("TASKS.json", catalog), ("SOURCE_IDENTITY_MAP.json", {"source_identities": book.get("source_identities", {}), "source_aliases": book.get("source_aliases", {})})):
        _write(input_dir / name, value)
    manifest = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "requested_route": route,
        "effective_route": route, "input_hash": input_hash, "input_path": str(input_dir / "FULL_BODY_INPUT.json"),
        "source_file_hashes": source_hashes, "prompt_file_hashes": prompt_hashes, "code_hash": code_hash,
        "config": config, "stages": [], "not_executed": [], "status": "planning",
        "execution_mode": getattr(factory, "execution_mode", "injected" if factory else "preview"),
        "automatic_paid_retries": False, "hidden_final_integration": False,
        "continue_after_unresolved_semantic_omission": True,
        "scope_assumptions": ["Complete approved BODY task coverage is the delivery unit; structural completion does not certify scientific quality.",
            "All evidence remains archived; selected model views and curator selections are explicitly recorded.",
            "Future requests require actual accepted prose; sequential preview is not a full-pool-every-call price estimate."]}
    segments, stages, dependencies, selections, partial = [], manifest["stages"], [], [], ""
    completed, issues = [], []

    def checkpoint():
        _write(run_dir / "RUN_MANIFEST.json", manifest)
        _write(output / "RUN_MANIFEST.json", manifest)
        _write(run_dir / "MATERIAL_SELECTION.json", selections)
        _write(run_dir / "PARTIAL_BODY.md", _join(segments) + ("\n\n" if segments and partial else "") + partial, text=True)

    def execute(stage, parser):
        if len(stages) >= config["max_author_calls"]:
            raise CandidateError("explicit_max_author_calls_reached")
        outcome = _execute_stage(stage, output=output, run_dir=run_dir, route=route,
            code_hash=code_hash, input_hash=input_hash, dependencies=deepcopy(dependencies),
            client_factory=factory, run=run, retry_failed=retry_failed, config=config, counter=counter, parser=parser)
        stages.append(outcome)
        checkpoint()
        if outcome.get("result_sha256"):
            dependencies.append(_dependency(outcome))
        return outcome

    def author(ids, index, reads=(), round_no=0):
        return _author_stage(pack, ids, route, segments, selections if route == "dossier_author" else None,
                             config, counter, index, reads, round_no)

    try:
        if route == "dossier_author":
            remaining = list(catalog)
            while remaining:
                cid = catalog[remaining[0]]["chapter_id"]
                scope = [key for key in remaining if catalog[key]["chapter_id"] == cid]
                def build_curator(ids, atom_ids=None, batch_no=None):
                    payload = writing_evidence.evidence_payload(pack, ids, atom_ids)
                    if batch_no is not None:
                        payload["evidence_batch"] = {"index": batch_no, "scope": "complete_semantic_atoms", "all_batches_required_before_author": True}
                    return _stage(f"curator_{len(selections)+1:03d}", "curator",
                        _messages(_prompt("curator"), payload),
                        config["curator"], config, counter, task_ids=list(ids),
                        evidence_atom_ids=[atom["atom_id"] for atom in payload["evidence_atoms"]], evidence_batch=batch_no)
                stage = build_curator(scope)
                if not stage["estimate"]["fits"] and not build_curator(scope, [])["estimate"]["fits"]:
                    # Split task intent only if even the evidence-free cluster
                    # cannot fit; shared source pools otherwise get curated once.
                    scope = _fit_scope(scope, lambda ids: build_curator(ids, []))["task_ids"]
                    stage = build_curator(scope)
                scope = stage["task_ids"]
                batches = [None]
                if not stage["estimate"]["fits"]:
                    # Even one complete task can have a large material pool.
                    # Partition only between intact atoms and inspect every batch.
                    atoms = [atom["atom_id"] for atom in writing_evidence.evidence_payload(pack, scope)["evidence_atoms"]]
                    batches, current = [], []
                    for aid in atoms:
                        if current and not build_curator(scope, [*current, aid], len(batches)+1)["estimate"]["fits"]:
                            batches.append(current)
                            current = []
                        current.append(aid)
                    if current:
                        batches.append(current)
                    if not batches:
                        batches = [None]
                cluster_complete = True
                for batch_no, atom_ids in enumerate(batches, 1):
                    stage = build_curator(scope, atom_ids, batch_no if atom_ids is not None else None)
                    outcome = execute(stage, _curator_parser(pack, scope, stage["evidence_atom_ids"]))
                    if outcome["status"] != "complete":
                        manifest["not_executed"].append({"reason": "writer_requires_all_valid_curator_batches", "task_ids": scope})
                        cluster_complete = False
                        break
                    selections.append({**outcome["result"], "evidence_batch": batch_no if atom_ids is not None else None,
                        "considered_atom_ids": stage["evidence_atom_ids"]})
                    checkpoint()
                if not cluster_complete:
                    break
                remaining = [key for key in remaining if key not in scope]
                checkpoint()
            if remaining:
                raise CandidateError("curator_selection_pending")
        remaining = list(catalog)
        composition = route == "packed_continuous"
        if route == "dossier_author":
            composition = not author(remaining, 1)["estimate"]["fits"]
        manifest["assembly_method"] = "natural_chapter_composition_with_actual_prefix" if composition else "single_full_body_author"
        manifest["composition_reason"] = "explicit_continuous_route" if route == "packed_continuous" else "selected_full_body_exceeds_actual_input_capacity" if composition else "full_body_fits"
        while remaining:
            cid = catalog[remaining[0]]["chapter_id"]
            candidates = [key for key in remaining if catalog[key]["chapter_id"] == cid] if composition else remaining
            stage = _fit_scope(candidates, lambda ids: author(ids, len(segments)+1)) if composition else author(candidates, 1)
            scope, reads = stage["task_ids"], []
            for read_round in range(config["max_rereads_per_window"] + 1):
                outcome = execute(stage, _author_parser(book, scope))
                parsed = outcome.get("result", {})
                if outcome["status"] != "complete" or parsed.get("kind") != "reread_request":
                    break
                requested = parsed["read_atom_ids"]
                if any(aid not in pack["atoms"] for aid in requested):
                    _reject_outcome(outcome, "unknown_reread_atom", "Unknown atom IDs were not substituted; inspect the recorded request.")
                    break
                for supplied in parsed["read_source_handles"]:
                    handle = pack["source_aliases"].get(supplied, supplied)
                    if handle not in pack["source_atom_ids"]:
                        _reject_outcome(outcome, "unknown_reread_source", "Unknown source handle was not substituted.")
                        break
                    requested = [*requested, *pack["source_atom_ids"][handle]]
                if outcome["status"] != "complete":
                    break
                fresh = [aid for aid in dict.fromkeys(requested) if aid not in stage["selected_atom_ids"]]
                if not fresh or read_round == config["max_rereads_per_window"]:
                    _reject_outcome(outcome, "reread_bound_or_no_progress", "No automatic retry: requested evidence was already supplied or the explicit reread bound was reached.")
                    break
                reads.extend(fresh)
                stage = author(scope, len(segments)+1, reads, read_round+1)
            parsed = outcome.get("result", {})
            partial = parsed.get("body_markdown", "")
            checkpoint()
            missing = parsed.get("pending_task_ids", [])
            provisional = bool(composition and partial and missing and parsed.get("kind") == "author"
                and parsed.get("diagnostics", {}).get("valid_response_envelope")
                and _normal_stage_transport(outcome))
            if (outcome["status"] == "pending" and partial and missing and config["completion_on_missing"]
                and parsed.get("transport_complete") and not parsed.get("call_error")
                and parsed.get("diagnostics", {}).get("valid_response_envelope")
                and not parsed.get("diagnostics", {}).get("transport_incomplete")):
                payload = _payload(pack, missing, segments)
                payload.update(existing_scope_body_markdown=partial, missing_task_ids=missing,
                               original_scope_task_ids=scope, completion_policy="insert_only_one_attempt")
                completion = _stage(f"completion_{len(segments)+1:03d}", "writer", _messages(_prompt("writer", "completion"), payload),
                    config["writer"], config, counter, task_ids=missing, original_body_sha256=_text_hash(partial))
                repair = execute(completion, _completion_parser(book, scope, partial, parsed, missing))
                if repair["status"] == "complete":
                    outcome, parsed = repair, repair["result"]
                    partial = parsed["body_markdown"]
                else:
                    provisional = provisional and repair["status"] == "pending" and _normal_stage_transport(repair)
                    manifest["not_executed"].append({"reason": "completion_pending_original_draft_preserved", "stage_id": repair["stage_id"]})
            accepted = outcome["status"] == "complete" and parsed.get("kind") != "reread_request"
            if not accepted and not provisional:
                if outcome["status"] == "planned":
                    manifest["not_executed"].append({"reason": "later_requests_require_actual_accepted_prose", "remaining_task_ids": remaining})
                break
            content = parsed["body_markdown"]
            segment_id = f"segment_{len(segments)+1:04d}"
            path = output / "manuscript" / _text_hash(content) / (segment_id + ".md")
            _write(path, content, text=True)
            segments.append({"segment_id": segment_id, "task_ids": scope,
                "chapter_ids": list(dict.fromkeys(catalog[key]["chapter_id"] for key in scope)),
                "body_markdown": content, "sha256": _text_hash(content), "full_text_path": str(path),
                "task_dispositions": [({**row, "status": "pending", "reported_status": row.get("status")}
                    if not accepted and row.get("task_id") in missing else row) for row in parsed.get("task_dispositions", [])],
                "accepted": accepted, "status": "complete" if accepted else "provisional_semantic_omission",
                "completed_task_ids": scope if accepted else parsed.get("completed_task_ids", []),
                "pending_task_ids": [] if accepted else missing,
                "issues": parsed.get("issues", []), **_dependency(outcome)})
            completed.extend(scope if accepted else parsed.get("completed_task_ids", []))
            if not accepted:
                manifest.setdefault("provisional_segments", []).append({"segment_id": segment_id, "pending_task_ids": missing,
                    "policy": "retain_exact_draft_and_continue_composition_without_declaring_missing_tasks_complete"})
            remaining = [key for key in remaining if key not in scope]
            partial = ""
            checkpoint()
    except Exception as exc:
        issues.append(_safe_error(exc))
        manifest["error"] = issues[-1]
    body = _join(segments) + ("\n\n" if segments and partial else "") + partial
    checked = parse_fullbody_response({"body_markdown": body, "completed_task_ids": completed,
        "complete": len(completed) == len(catalog)}, book)
    complete = checked["complete"] and not issues
    manifest["status"] = "complete" if complete else "preview" if not run else "pending"
    calls = sum(stage.get("model_calls", 0) for stage in stages)
    paid = None if any(stage.get("paid_dispatch_count") is None for stage in stages) else sum(stage.get("paid_dispatch_count", 0) for stage in stages)
    cost = _cost_summary(stages, None)
    result = {**checked, "schema_version": SCHEMA_VERSION + ".result", "complete": complete,
        "body_complete": bool(complete), "body_markdown": body, "body_sha256": _text_hash(body),
        "issues": [*checked["issues"], *issues], "segments": segments, "partial_unaccepted_prose": bool(partial or any(not row.get("accepted", True) for row in segments)),
        "task_dispositions": [row for segment in segments for row in segment.get("task_dispositions", [])],
        "completed_task_ids": completed, "pending_task_ids": [key for key in catalog if key not in completed],
        "run_id": run_id, "input_hash": input_hash, "input_path": str(input_dir / "FULL_BODY_INPUT.json"),
        "requested_route": route, "effective_route": route, "status": manifest["status"],
        "assembly_method": manifest.get("assembly_method"), "semantic_quality_unreviewed": True,
        "material_preserved": True, "material_preservation_scope": "complete_archive;model_view_is_explicit_semantic_projection", "evidence_archive_path": str(input_dir / "WRITING_EVIDENCE.json"),
        "material_selection_path": str(run_dir / "MATERIAL_SELECTION.json"),
        "source_identity_map_path": str(input_dir / "SOURCE_IDENTITY_MAP.json"),
        "approved_chapter_order": [chapter["chapter_id"] for chapter in book["chapters"]],
        "execution_mode": manifest["execution_mode"], "output_dir": str(output), "model_calls": calls,
        "client_invocations": calls, "paid_dispatch_count": paid, "cost_summary": cost,
        "stage_lineage": [{**_dependency(stage), "role": stage["role"], "status": stage["status"]} for stage in stages]}
    _write(run_dir / "MANUSCRIPT_SEGMENTS.json", segments)
    _write(run_dir / "FULL_BODY_RESULT.json", result)
    _write(run_dir / "FULL_BODY.md", body, text=True)
    selected = result
    if (output / "FULL_BODY_RESULT.json").exists():
        try:
            previous = _read(output / "FULL_BODY_RESULT.json")
            if previous.get("complete") and not complete:
                selected = previous
        except (OSError, ValueError):
            pass
    if selected is result:
        _write(output / "FULL_BODY_RESULT.json", result)
        _write(output / "FULL_BODY.md", body, text=True)
    manifest.update(model_calls=calls, paid_dispatch_count=paid, cost_summary=cost,
        selected_version=selected["run_id"], selected_result_path=str(output / "runs" / selected["run_id"] / "FULL_BODY_RESULT.json"),
        selected_input_matches_current=selected.get("input_hash") == input_hash)
    checkpoint()
    public = [{k: v for k, v in stage.items() if k != "result"} for stage in stages]
    _write(run_dir / "IMPLEMENTATION_REPORT.json", {**manifest, "stages": public})
    _write(output / "IMPLEMENTATION_REPORT.json", {**manifest, "stages": public})
    return {**result, "stages": public, **{key: manifest[key] for key in ("selected_version", "selected_result_path", "selected_input_matches_current")}}
