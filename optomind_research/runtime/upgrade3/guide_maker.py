"""Outline-to-guide authoring with model-directed, intact scientific reads.

A guide is an author artifact, not a task mapping. The runtime only enforces
transport, source identity, bounded reads, and available request capacity.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import time
from typing import Mapping
import uuid

from .fullbody_writer import (_cost_summary, _dependency, _execute_stage, _messages, _stage, _text_hash,
                              _reject_outcome)
from .writer_candidates import ROOT, _git_commit, _hash, _output_lock, _profile, _read, _safe_error, _write
from .writer_candidates_contracts import CandidateError
from .guided_body_contracts import validate_guide
from .guide_maker_contracts import (compile_guide_input, build_maker_payload, parse_maker_response,
                                    resolve_material_requests, decode_maker_response)

SCHEMA_VERSION = "optomind.guide_maker.v1"
PROMPT_ROOT = ROOT / "prompts" / "guide_maker"


def validate_config(config):
    if not isinstance(config, Mapping):
        raise CandidateError("config_must_be_object")
    allowed = {"schema_version", "purpose", "maker", "max_input_tokens", "max_model_calls"}
    if set(config) - allowed:
        raise CandidateError("unknown_config_keys:" + ",".join(sorted(set(config) - allowed)))
    if config.get("schema_version", "optomind.guide_maker_config.v1") != "optomind.guide_maker_config.v1":
        raise CandidateError("guide_maker_config_schema_invalid")
    result = deepcopy(dict(config))
    if not isinstance(config.get("maker", {}), Mapping):
        raise CandidateError("profile_must_be_object:maker")
    result["maker"] = _profile(config.get("maker", {}), "maker")
    for key, default in (("max_input_tokens", None), ("max_model_calls", 8)):
        value = config.get(key, default)
        if value is None and key == "max_input_tokens":
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise CandidateError(key + "_must_be_positive_integer")
        result[key] = value
    return result


def _source_file_hashes():
    names = ("guide_maker.py", "guide_maker_contracts.py", "json_format_recovery.py", "chapter_arrangement.py", "guided_body_contracts.py", "writing_evidence.py",
             "fullbody_writer.py", "writer_candidates.py", "writer_candidates_contracts.py", "module4/runtime.py")
    paths = [Path(__file__).parent / name for name in names]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def render_guide(guide):
    parts = ["# Writing guide", guide["manuscript_guide"]]
    for chapter in guide["chapters"]:
        parts.extend(["## " + chapter["chapter_id"] + ": " + chapter["title"], chapter["writing_arrangement"]])
        if chapter.get("required_content"):
            parts.append("\n".join("- " + value for value in chapter["required_content"]))
        if chapter.get("source_handles"):
            parts.append("Sources: " + ", ".join(chapter["source_handles"]))
    return "\n\n".join(parts) + "\n"


def persist_format_recovery(directory, parsed, raw_path=None):
    """Write derived syntax evidence only; never change the provider response."""
    audit = parsed.get("format_recovery")
    if not audit:
        return None
    directory = Path(directory)
    record = deepcopy(audit)
    normalized = record.pop("normalized_text")
    candidate = directory / "NORMALIZED_RESPONSE.json"
    _write(candidate, normalized, text=True)
    record.update(schema_version="optomind.guide_format_recovery.v1",
        normalized_candidate_path=str(candidate), semantic_quality_unreviewed=True,
        scientific_review_performed=False, provider_response_modified=False)
    if raw_path is not None and Path(raw_path).is_file():
        record.update(raw_response_path=str(raw_path),
            raw_response_file_sha256=hashlib.sha256(Path(raw_path).read_bytes()).hexdigest())
    _write(directory / "FORMAT_RECOVERY.json", record)
    return str(directory / "FORMAT_RECOVERY.json")


def run_guide_maker(book, output_dir, config, client_factory=None, run=False,
                    retry_failed=False, token_counter=None, feedback=None):
    output = Path(output_dir).resolve()
    with _output_lock(output):
        return _run(book, output, config, client_factory, run, retry_failed, token_counter, feedback)


def _run(book, output, config, client_factory, run, retry_failed, counter, feedback):
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:10]
    run_dir = output / "runs" / run_id
    run_dir.mkdir(parents=True)
    input_hash = _hash({"book": book, "feedback": feedback})
    input_path = output / "inputs" / input_hash / "FULL_BODY_INPUT.json"
    _write(input_path, book)
    mode = getattr(client_factory, "execution_mode", "injected" if client_factory else "preview")
    manifest = dict(schema_version=SCHEMA_VERSION, run_id=run_id, input_hash=input_hash,
        input_path=str(input_path), execution_mode=mode, run=bool(run), stages=[], status="planning",
        git_commit=_git_commit(), material_preserved=True, automatic_paid_retries=False,
        semantic_quality_unreviewed=True, requested_route="guide_maker", effective_route="guide_maker")
    guide, queue, history, trace, needs, issues = None, [], [], [], [], []
    dependencies, seen, rereads = [], set(), set()
    status, action = "pending", None

    def checkpoint():
        _write(run_dir / "RUN_MANIFEST.json", manifest)
        _write(output / "RUN_MANIFEST.json", manifest)
        _write(run_dir / "READ_TRACE.json", trace)
        if guide is not None:
            _write(run_dir / "DRAFT_GUIDE.json", guide)
            _write(output / "DRAFT_GUIDE.json", guide)
            _write(run_dir / "DRAFT_GUIDE.md", render_guide(guide), text=True)
            _write(output / "DRAFT_GUIDE.md", render_guide(guide), text=True)

    try:
        config = validate_config(config)
        bundle = compile_guide_input(book, feedback=feedback)
        hashes = _source_file_hashes()
        prompt = (PROMPT_ROOT / "generate.md").read_text(encoding="utf-8")
        manifest.update(config=config, source_file_hashes=hashes, code_hash=_hash(hashes),
                        prompt_file_hashes={"generate.md": _text_hash(prompt)})
        _write(run_dir / "INPUT_PROVENANCE.json", {"input_hash": input_hash, "feedback": feedback,
                                                   "source_file_hashes": hashes})

        def parser(response):
            try:
                parsed = parse_maker_response(response, book, bundle)
            except Exception as exc:
                # Invalid requests cannot discard an otherwise valid full draft,
                # but are never repaired into a successful cache entry or final.
                obj, transport, audit = decode_maker_response(response)
                try:
                    provisional = validate_guide(obj.get("guide"), book)
                except (ValueError, TypeError, AttributeError):
                    provisional = None
                parsed = dict(guide=provisional, reading_needs=[], changes=[], complete=False,
                              errors=[str(exc)], transport_complete=transport)
                if audit:
                    parsed["format_recovery"] = audit
            parsed["maker_transport_complete"] = parsed.get("transport_complete", True)
            parsed["guide_complete"] = parsed.get("complete", False)
            parsed["complete"] = bool(parsed.get("guide") and not parsed.get("errors")
                                      and parsed.get("transport_complete", True))
            return parsed

        for index in range(1, config["max_model_calls"] + 1):
            before_hash = _hash(guide) if guide is not None else None
            prior = deepcopy(guide)
            def make_stage(batch):
                payload = build_maker_payload(bundle, prior_guide=guide, needs=needs,
                    materials=batch, read_history=history, feedback=feedback)
                return _stage(f"maker_{index:03d}", "maker", _messages(prompt, payload), config["maker"],
                    config, counter, guide_before_sha256=before_hash, material_preserved=True)
            batch = []
            stage = make_stage(batch)
            if stage["estimate"]["fits"]:
                for unit in queue:
                    candidate = make_stage(batch + [unit])
                    if not candidate["estimate"]["fits"]:
                        if not batch:
                            stage = candidate  # Archive the intact blocked packet.
                        break
                    batch.append(unit)
                    stage = candidate
            if not stage["estimate"]["fits"]:
                stage["required_action"] = "The full outline, current guide and an intact requested source packet exceed capacity. Check the exact tokenizer and remove only redundant non-scientific projection metadata, or select a model with a larger supported context window. Do not raise limits beyond the model window. No truncation was performed."
            outcome = _execute_stage(stage, output=output, run_dir=run_dir, route="guide_maker",
                code_hash=manifest["code_hash"], input_hash=input_hash, dependencies=deepcopy(dependencies),
                client_factory=client_factory, run=run, retry_failed=retry_failed, config=config,
                counter=counter, parser=parser)
            # Direct synthetic JSON complete:false is a provisional guide, not
            # a provider transport failure. Never override provider envelopes.
            parsed = outcome.get("result", {})
            if parsed.get("guide") and not parsed.get("transport_complete") and not parsed.get("call_error") and outcome.get("attempt_dir"):
                raw = _read(Path(outcome["attempt_dir"]) / "RAW_RESPONSE.json")
                if isinstance(raw, Mapping) and "guide" in raw and "content" not in raw:
                    repaired = parser(raw)
                    if repaired.get("transport_complete"):
                        outcome.update(result=repaired, result_sha256=_hash(repaired),
                                       status="complete" if repaired.get("complete") else "pending")
                        _write(Path(outcome["attempt_dir"]) / "RESULT.json", repaired)
            # The shared stage wrapper has a shallow transport check. Retain
            # the guide parser's nested-envelope decision without changing it.
            parsed = outcome.get("result", {})
            if parsed.get("maker_transport_complete") is False:
                parsed.update(transport_complete=False, complete=False)
                outcome.update(status="pending", result_sha256=_hash(parsed))
                if outcome.get("attempt_dir"):
                    _write(Path(outcome["attempt_dir"]) / "RESULT.json", parsed)
            if outcome.get("attempt_dir") and parsed.get("format_recovery"):
                attempt = Path(outcome["attempt_dir"])
                outcome["format_recovery_path"] = persist_format_recovery(
                    attempt, parsed, attempt / "RAW_RESPONSE.json")
            manifest["stages"].append(outcome)
            checkpoint()
            if "result" not in outcome:
                status = "preview" if outcome["status"] == "planned" else outcome["status"]
                action = outcome.get("required_action", outcome.get("error"))
                break
            parsed = outcome["result"]
            dependencies.append(_dependency(outcome))
            if parsed.get("transport_complete") and parsed.get("guide"):
                guide = validate_guide(parsed["guide"], book)
                checkpoint()
            if not parsed.get("transport_complete") or not parsed.get("complete"):
                status = "transport_failed" if not parsed.get("transport_complete") else "response_invalid"
                issues.extend(parsed.get("errors") or parsed.get("issues", []))
                break
            guide = validate_guide(parsed["guide"], book)
            queue = queue[len(batch):]
            records = []
            for unit in batch:
                packet_hash = unit["packet_sha256"]
                archive = output / "reads" / packet_hash / "SOURCE_PACKET.json"
                if not archive.exists():
                    _write(archive, unit)
                record = {key: deepcopy(unit[key]) for key in
                    ("source_handle", "need_ids", "questions", "packet_sha256", "reread_reasons") if key in unit}
                record.update(stage_id=outcome["stage_id"], archive_path=str(archive))
                records.append(record)
                history.append({key: value for key, value in record.items() if key != "archive_path"})
                seen.add(unit["source_handle"])
            trace.append(dict(stage_id=outcome["stage_id"], guide_before_sha256=before_hash,
                guide_after_sha256=_hash(guide),
                changed_chapter_ids=[c["chapter_id"] for c in guide["chapters"]
                    if c != next((old for old in (prior or {}).get("chapters", []) if old["chapter_id"] == c["chapter_id"]), None)],
                manuscript_guide_changed=(prior or {}).get("manuscript_guide") != guide["manuscript_guide"],
                reads=records, reading_needs=deepcopy(parsed["reading_needs"]),
                changes=deepcopy(parsed["changes"])))
            checkpoint()
            requested = resolve_material_requests(bundle, parsed["reading_needs"])
            queued = {unit["source_handle"] for unit in queue}
            pending_ids = {ident for unit in queue for ident in unit["need_ids"]}
            for unit in requested:
                handle = unit["source_handle"]
                if handle in queued:
                    continue
                if handle in seen:
                    reasons = tuple(reason for reason in unit.get("reread_reasons", []) if isinstance(reason, str) and reason.strip())
                    if not reasons:
                        if pending_ids.intersection(unit["need_ids"]):
                            continue
                        reason = "repeated_read_request:" + handle + ":explicit_new_reread_reason_required"
                        _reject_outcome(outcome, "repeated_read_request", reason)
                        raise CandidateError(reason)
                    reread_key = (handle, reasons)
                    if reread_key in rereads:
                        reason = "repeated_reread_purpose:" + handle
                        _reject_outcome(outcome, "repeated_reread_purpose", reason)
                        raise CandidateError(reason)
                    rereads.add(reread_key)
                queue.append(unit)
                queued.add(handle)
            active_ids = {ident for unit in queue for ident in unit["need_ids"]}
            by_id = {need["need_id"]: need for need in needs + parsed["reading_needs"]}
            needs = [value for key, value in by_id.items() if key in active_ids]
            if parsed.get("guide_complete") and not queue:
                status = "complete"
                break
            if not queue:
                status, action = "incomplete", "The model retained a provisional guide without a new reading request. Inspect the draft or explicitly retry."
                break
        else:
            status, action = "model_call_limit", "Increase max_model_calls to continue from the cached provisional guide and queued intact reads."
    except Exception as exc:
        status = "blocked"
        issues.append(_safe_error(exc))
    complete = status == "complete"
    calls = sum(row.get("model_calls", 0) for row in manifest["stages"])
    paid = None if any(row.get("paid_dispatch_count") is None for row in manifest["stages"]) else sum(row.get("paid_dispatch_count", 0) for row in manifest["stages"])
    cost = _cost_summary(manifest["stages"], None)
    if mode in ("recording", "replay", "preview"):
        cost.update(candidate_stage_known_cost_cny=0, total_known_cost_including_base_cny=0,
            candidate_stage_cost_complete=True, total_cost_complete=True, unknown_cost_attempts=[],
            accounting_note="Offline replay/recording/preview has no provider charge.")
        for record in cost["all_attempts_including_retries"]:
            record["estimated_actual_cost_cny"] = 0
    result = dict(schema_version=SCHEMA_VERSION + ".result", run_id=run_id, status=status, complete=complete,
        guide=guide, guide_sha256=_hash(guide) if guide is not None else None, issues=issues,
        required_action=action, input_hash=input_hash, input_path=str(input_path), output_dir=str(output),
        outstanding_reading_needs=needs, queued_source_handles=[unit["source_handle"] for unit in queue],
        read_history=history, read_trace=trace, model_calls=calls, client_invocations=calls,
        paid_dispatch_count=paid, execution_mode=mode, cost_summary=cost,
        requested_route="guide_maker", effective_route="guide_maker", material_preserved=True,
        semantic_quality_unreviewed=True)
    _write(run_dir / "GUIDE_RESULT.json", result)
    if complete:
        _write(run_dir / "GUIDE.json", guide)
        _write(run_dir / "GUIDE.md", render_guide(guide), text=True)
    selected = result
    existing = output / "GUIDE_RESULT.json"
    if existing.exists():
        previous = _read(existing)
        if previous.get("complete") and not complete:
            selected = previous
            manifest["previous_complete_version_preserved"] = True
    if selected is result:
        _write(existing, result)
        if complete:
            _write(output / "GUIDE.json", guide)
            _write(output / "GUIDE.md", render_guide(guide), text=True)
    manifest.update(status=status, model_calls=calls, client_invocations=calls, paid_dispatch_count=paid,
        cost_summary=cost, selected_version=selected["run_id"], current_run_version=run_id,
        selected_input_matches_current=selected.get("input_hash") == input_hash,
        selected_result_path=str(output / "runs" / selected["run_id"] / "GUIDE_RESULT.json"))
    checkpoint()
    _write(output / "READ_TRACE.json", trace)
    public = [{key: value for key, value in row.items() if key != "result"} for row in manifest["stages"]]
    report = {**manifest, "stages": public}
    _write(run_dir / "IMPLEMENTATION_REPORT.json", report)
    _write(output / "IMPLEMENTATION_REPORT.json", report)
    return {**result, "stages": public, **{key: manifest[key] for key in
        ("selected_version", "selected_result_path", "selected_input_matches_current")}}
