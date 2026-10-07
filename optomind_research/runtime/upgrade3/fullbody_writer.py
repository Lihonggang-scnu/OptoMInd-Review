"""Complete-BODY authoring with auditable continuation, rereads and local edits.

The unit of delivery is one complete BODY, never a chapter renamed as a BODY.
Tasks select complete evidence, not visible paragraphs. Each dispatched call
uses the common transport/ledger, and uncertain attempts are never retried
implicitly. No route has a hidden final integration or memory-generation call.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence
import uuid

from .module4.runtime import estimated_cost_cny, invoke_client
from .writer_candidates import (ROOT, _check_client, _estimate, _git_commit,
                                _hash, _output_lock, _profile, _read,
                                _safe_error, _write)
from .writer_candidates_contracts import CandidateError, _handles
from .chapter_arrangement import _escape_inner_json_quotes
from .fullbody_contracts import (fullbody_task_catalog, parse_fullbody_response,
                                 project_fullbody)

SCHEMA_VERSION = "optomind.fullbody_writer.v1"
PLAIN_ROUTES = ("plain_whole", "chapter_concat", "hierarchical_full")
ROUTES = ("whole_author", "continuous_author", "workbench", "reader_revision", *PLAIN_ROUTES)
PROMPT_ROOT = ROOT / "prompts" / "fullbody_writer"


def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize every effective role parameter before any client is created."""
    if not isinstance(config, Mapping):
        raise CandidateError("config_must_be_object")
    allowed = {"schema_version", "purpose", "writer", "reader", "reviser", "max_input_tokens",
               "max_tasks_per_window", "recent_prose_segments", "max_rereads_per_window",
               "max_author_calls", "whole_body_output_tokens"}
    if set(config) - allowed:
        raise CandidateError("unknown_config_keys:" + ",".join(sorted(set(config) - allowed)))
    result = deepcopy(dict(config))
    for role in ("writer", "reader", "reviser"):
        raw = config.get(role, {})
        if not isinstance(raw, Mapping):
            raise CandidateError("profile_must_be_object:" + role)
        result[role] = _profile(raw, role)
    defaults = {"recent_prose_segments": 2, "max_rereads_per_window": 8, "max_author_calls": 256}
    for key in ("max_input_tokens", "max_tasks_per_window", "recent_prose_segments",
                "max_rereads_per_window", "max_author_calls", "whole_body_output_tokens"):
        value = config.get(key, defaults.get(key))
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise CandidateError(key + "_must_be_positive_integer")
        result[key] = value
    return result


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source_file_hashes() -> dict[str, str]:
    paths = [Path(__file__), Path(__file__).with_name("fullbody_contracts.py"),
             Path(__file__).with_name("writer_candidates.py"),
             Path(__file__).with_name("writer_candidates_contracts.py"),
             Path(__file__).with_name("review_unit_writer.py"),
             Path(__file__).with_name("chapter_arrangement.py"),
             Path(__file__).parent / "module4/runtime.py"]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def _prompt(*names: str) -> str:
    return "\n\n".join((PROMPT_ROOT / (name + ".md")).read_text(encoding="utf-8") for name in names)


def _messages(prompt: str, payload: Mapping[str, Any]) -> list[dict[str, str]]:
    return [{"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)}]


def _transport_complete(response: Any) -> bool:
    return not (isinstance(response, Mapping) and
                (response.get("complete") is False or response.get("finish_reason") in
                 ("length", "content_filter", "error", "timeout", "cancelled")))


def _object(response: Any) -> dict[str, Any]:
    """Strict JSON protocol for navigation/reader/patches, with fenced JSON support."""
    value = response
    if isinstance(value, Mapping) and "content" in value:
        value = value["content"]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("```") and text.endswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            value = json.loads(text, strict=False)
        except ValueError:
            repaired = _escape_inner_json_quotes(text)
            value = json.loads(repaired, strict=False)
    if not isinstance(value, Mapping):
        raise CandidateError("response_must_be_json_object")
    return deepcopy(dict(value))


def _author_parser(book: Mapping[str, Any], ids: Sequence[str], *, allow_reads: bool, prefix: str = ""):
    def parse(response: Any) -> dict[str, Any]:
        if allow_reads:
            try:
                obj = _object(response)
            except (ValueError, TypeError):
                obj = {}
            if "read_segment_ids" in obj or "read_source_handles" in obj:
                requested = obj.get("read_segment_ids", [])
                sources = obj.get("read_source_handles", [])
                if (not isinstance(requested, list) or not isinstance(sources, list) or not (requested or sources) or
                    any(not isinstance(x, str) or not x for x in requested + sources) or
                    len(requested) != len(set(requested)) or len(sources) != len(set(sources)) or obj.get("body_markdown")):
                    raise CandidateError("invalid_full_prose_reread_request")
                return {"kind": "reread_request", "read_segment_ids": requested, "read_source_handles": sources,
                        "complete": _transport_complete(response), "body_markdown": ""}
        parsed = parse_fullbody_response(response, book, list(ids))
        if prefix and parsed.get("body_markdown"):
            # Coverage sidecars describe the entire current scope, including the
            # saved exact prefix; table validation must inspect that same scope.
            combined_envelope = {"body_markdown": prefix + parsed["body_markdown"],
                                 "task_dispositions": parsed.get("task_dispositions", [])}
            self_claim = parsed.get("diagnostics", {}).get("body_complete_self_claim")
            if self_claim is not None:
                combined_envelope["complete"] = self_claim
            combined = parse_fullbody_response(combined_envelope, book, list(ids))
            if not parsed.get("diagnostics", {}).get("transport_incomplete"):
                parsed.update(complete=combined["complete"], completed_task_ids=combined["completed_task_ids"],
                              pending_task_ids=combined["pending_task_ids"], diagnostics=combined["diagnostics"], issues=combined["issues"])
        parsed["kind"] = "author"
        return parsed
    return parse


def _reader_parser(original: str):
    def parse(response: Any) -> dict[str, Any]:
        obj = _object(response)
        issues = obj.get("issues")
        if not isinstance(issues, list):
            raise CandidateError("reader_issues_must_be_list")
        valid, pending, seen = [], [], set()
        reserved_ids = {issue.get("issue_id") for issue in issues if isinstance(issue, Mapping)
                        and isinstance(issue.get("issue_id"), str) and issue["issue_id"].strip()}
        for index, raw in enumerate(issues, 1):
            if not isinstance(raw, Mapping):
                pending.append({"issue_id": f"reader_issue_{index:04d}", "reported_issue": deepcopy(raw),
                                "validation_errors": ["reader_issue_must_be_object"]})
                continue
            issue = deepcopy(dict(raw))
            ident = issue.get("issue_id")
            if not isinstance(ident, str) or not ident.strip() or ident in seen:
                generated = f"reader_issue_{index:04d}"
                while generated in seen or generated in reserved_ids:
                    generated = "_" + generated
                if ident is not None:
                    issue["reported_issue_id"] = ident
                issue.update(issue_id=generated, issue_id_assigned_deterministically=True)
            seen.add(issue["issue_id"])
            errors = []
            for key in ("anchor", "problem"):
                if not isinstance(issue.get(key), str) or not issue[key].strip():
                    errors.append("reader_issue_missing:" + key)
            if not errors and original.count(issue["anchor"]) != 1:
                errors.append("reader_anchor_must_match_original_exactly_once")
            if errors:
                pending.append({**issue, "validation_errors": errors})
            else:
                # Supplementary explanations improve the diagnostic but their
                # absence cannot erase another useful, precisely located issue.
                valid.append(issue)
        return {**obj, "issues": valid, "pending_reader_issues": pending,
                "reported_issue_count": len(issues), "kind": "reader",
                "complete": obj.get("complete", True) is True and _transport_complete(response)}
    return parse


def _patch_parser(original: str, issues: Sequence[Mapping[str, Any]]):
    expected = {row["issue_id"]: row for row in issues}

    def parse(response: Any) -> dict[str, Any]:
        obj = _object(response)
        patches = obj.get("patches", [])
        rejected = obj.get("rejected_issues", [])
        if not isinstance(patches, list) or not isinstance(rejected, list):
            raise CandidateError("revision_patches_and_rejections_must_be_lists")
        addressed = set()
        spans = []
        for patch in patches:
            if not isinstance(patch, Mapping):
                raise CandidateError("revision_patch_must_be_object")
            ident = patch.get("issue_id")
            if ident not in expected:
                raise CandidateError("revision_unknown_issue")
            anchor = patch.get("anchor")
            if not isinstance(anchor, str) or not anchor or original.count(anchor) != 1:
                raise CandidateError("revision_anchor_nonunique:" + ident)
            if anchor != expected[ident]["anchor"] and (not isinstance(patch.get("reason"), str) or not patch["reason"].strip()):
                raise CandidateError("revision_related_anchor_requires_reason:" + ident)
            if not isinstance(patch.get("replacement"), str):
                raise CandidateError("revision_replacement_must_be_string")
            start = original.index(patch["anchor"])
            spans.append((start, start + len(patch["anchor"]), ident))
            addressed.add(ident)
        for item in rejected:
            if (not isinstance(item, Mapping) or item.get("issue_id") not in expected or
                item["issue_id"] in addressed or not isinstance(item.get("reason"), str) or not item["reason"].strip()):
                raise CandidateError("invalid_revision_rejection")
            addressed.add(item["issue_id"])
        ordered = sorted(spans)
        if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
            raise CandidateError("overlapping_revision_anchors")
        if addressed != set(expected):
            raise CandidateError("revision_has_unaddressed_reader_issues")
        return {**obj, "patches": patches, "rejected_issues": rejected, "kind": "revision",
                "complete": obj.get("complete") is True and _transport_complete(response)}
    return parse


def _parse(response: Any, parser: Callable, error: Any = None) -> dict[str, Any]:
    try:
        result = parser(response)
    except Exception as exc:
        result = {"complete": False, "body_markdown": "", "issues": [
            {"code": "response_parse_failed", "error_type": type(exc).__name__, "detail": str(exc)}]}
    result["transport_complete"] = not error and _transport_complete(response)
    if not result["transport_complete"]:
        result["complete"] = False
    if error:
        result["call_error"] = error
        result.setdefault("issues", []).append({"code": "stage_call_failed", **error})
    return result


def _stage(stage_id: str, role: str, messages: list[dict[str, str]], profile: dict[str, Any],
           config: Mapping[str, Any], counter: Any, **extra: Any) -> dict[str, Any]:
    estimate = _estimate(messages, profile, config.get("max_input_tokens"), counter)
    return {"stage_id": stage_id, "role": role, "messages": messages,
            "effective_profile": profile, "estimate": estimate,
            "status": "ready" if estimate["fits"] else "capacity_blocked", **extra}


def _execute_stage(stage: dict[str, Any], *, output: Path, run_dir: Path, route: str,
                   code_hash: str, input_hash: str, dependencies: list[dict[str, Any]],
                   client_factory: Any, run: bool, retry_failed: bool, config: Mapping[str, Any],
                   counter: Any, parser: Callable) -> dict[str, Any]:
    signature = {"schema_version": SCHEMA_VERSION, "stage_id": stage["stage_id"], "route": route,
                 "execution_mode": getattr(client_factory, "execution_mode", "injected" if client_factory else "preview"),
                 "recording_fixture_sha256": getattr(client_factory, "fixture_sha256", None),
                 "messages": stage["messages"], "effective_profile": stage["effective_profile"],
                 "wire_request_sha256": stage["estimate"]["wire_request_sha256"],
                 "code_hash": code_hash, "dependencies": dependencies}
    key = _hash(signature)
    plan_dir = run_dir / "plans" / stage["stage_id"]
    _write(plan_dir / "MESSAGES.json", stage["messages"])
    public = {k: v for k, v in stage.items() if k != "messages"}
    public.update(cache_key=key, dependencies=dependencies, messages_path=str(plan_dir / "MESSAGES.json"),
                  model_calls=0, client_invocations=0, paid_dispatch_count=0)
    _write(plan_dir / "REQUEST_PREVIEW.json", {**public, "signature": {k: v for k, v in signature.items() if k != "messages"}})
    if stage["status"] == "capacity_blocked":
        return public
    stage_root = output / "stages" / stage["stage_id"] / key
    attempts = sorted(stage_root.glob("attempt_*")) if stage_root.exists() else []
    # Prefer any verified complete attempt, including one predating an interrupted
    # retry. Recovery from saved raw bytes is local and never dispatches a call.
    cached = []
    for attempt in attempts:
        result_path = attempt / "RESULT.json"
        if not result_path.exists() and (attempt / "RAW_RESPONSE.json").exists():
            error = _read(attempt / "ERROR.json") if (attempt / "ERROR.json").exists() else None
            _write(result_path, _parse(_read(attempt / "RAW_RESPONSE.json"), parser, error))
        if result_path.exists():
            cached.append((attempt, _read(result_path)))
    chosen = next(((path, result) for path, result in reversed(cached) if result.get("complete")), None)
    if chosen is None and attempts and not retry_failed:
        chosen = next(((path, result) for path, result in reversed(cached) if path == attempts[-1]), None)
        if chosen is None:
            return {**public, "status": "uncertain_attempt", "attempt_dir": str(attempts[-1]),
                    "required_action": "Inspect the interrupted request and shared ledger; --retry-failed explicitly authorizes another charged attempt. Uncertain holds are retained."}
    if chosen:
        attempt, result = chosen
        return {**public, "status": "complete" if result.get("complete") else "pending", "cache_hit": True,
                "attempt_dir": str(attempt), "result": result, "result_sha256": _hash(result),
                "usage": _read(attempt / "USAGE.json") if (attempt / "USAGE.json").exists() else None}
    if not run:
        return {**public, "status": "planned"}
    if client_factory is None:
        return {**public, "status": "blocked", "error": "client_factory_required_for_run"}
    attempt = stage_root / f"attempt_{len(attempts) + 1:03d}"
    attempt.mkdir(parents=True, exist_ok=False)
    call_id = stage["stage_id"] + "-" + key[:12] + "-" + attempt.name
    _write(attempt / "MESSAGES.json", stage["messages"])
    _write(attempt / "REQUEST.json", {**public, "call_id": call_id, "signature": {k: v for k, v in signature.items() if k != "messages"}})
    profile = stage["effective_profile"]
    error, response, invoked = None, None, False
    try:
        client = client_factory(stage["role"], attempt, deepcopy(profile))
        _check_client(client, profile)
        actual_profile = {**profile}
        for name in ("prompt_token_multiplier", "prompt_token_framing_margin"):
            actual_profile[name] = getattr(client, name, profile[name])
        actual = _estimate(stage["messages"], actual_profile, config.get("max_input_tokens"), getattr(client, "prompt_token_counter", counter))
        _write(attempt / "ACTUAL_REQUEST.json", {"profile": actual_profile, "estimate": actual})
        if not actual["fits"]:
            raise CandidateError("actual_client_meter_requires_batching:full_input_preserved")
        invoked = True
        response = invoke_client(client, stage["messages"], stage_id=stage["stage_id"], call_id=call_id,
            **{k: profile[k] for k in ("model", "thinking", "thinking_budget", "max_output_tokens",
                                     "json_mode", "timeout_seconds", "stream", "stream_overall_timeout_seconds")})
    except Exception as exc:
        error = _safe_error(exc)
        response = error.get("record") or {"content": "", "complete": False}
        _write(attempt / "ERROR.json", error)
    _write(attempt / "RAW_RESPONSE.json", response)  # Always before parsing.
    result = _parse(response, parser, error)
    usage = response.get("usage") if isinstance(response, Mapping) else None
    usage_record = {"usage": usage, "usage_known": bool(usage), "model": profile["model"],
                    "provider_request_id": response.get("request_id") if isinstance(response, Mapping) else None,
                    "effective_request": response.get("effective_request") if isinstance(response, Mapping) else None}
    if usage:
        try:
            usage_record["estimated_actual_cost_cny"] = estimated_cost_cny(usage, model=profile["model"])
        except Exception:
            usage_record["estimated_actual_cost_cny"] = None
    _write(attempt / "USAGE.json", usage_record)
    _write(attempt / "RESULT.json", result)
    _write(attempt / "BODY.md", result.get("body_markdown", ""), text=True)
    pre_dispatch = bool(error and any(x in error["error"] for x in ("global_budget_exceeded", "actual_client_meter_requires_batching",
        "client_profile_mismatch", "automatic_paid_retries", "reserved_attempt_cny_too_low", "missing_api_key", "credential")))
    paid = (0 if not invoked or pre_dispatch or signature["execution_mode"] != "live" else
            1 if isinstance(response, Mapping) and response.get("request_id") else None)
    return {**public, "status": "complete" if result.get("complete") else "pending", "cache_hit": False,
            "attempt_dir": str(attempt), "result": result, "result_sha256": _hash(result),
            "model_calls": int(invoked), "client_invocations": int(invoked), "paid_dispatch_count": paid,
            "call_error": error, "usage": usage_record}


def _reject_outcome(outcome: dict[str, Any], status: str, reason: str) -> None:
    """Persist a post-parse protocol failure so explicit retry can repair it."""
    outcome.update(status=status, required_action=reason)
    result = outcome.get("result")
    if isinstance(result, dict):
        result["complete"] = False
        result.setdefault("issues", []).append({"code": status, "detail": reason})
        outcome["result_sha256"] = _hash(result)
        if outcome.get("attempt_dir"):
            _write(Path(outcome["attempt_dir"]) / "RESULT.json", result)


def _join(segments: Sequence[Mapping[str, Any]]) -> str:
    return "\n\n".join(row["body_markdown"] for row in segments if row.get("body_markdown"))


def _navigation(segments: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in row.items() if k != "body_markdown"} for row in segments]


def _full_source_records(book: Mapping[str, Any], handles: Sequence[str]) -> list[dict[str, Any]]:
    """Retrieve all complementary approved records plus explicit source lineage."""
    identities, aliases = book.get("source_identities", {}), book.get("source_aliases", {})
    selected, pending = set(), list(handles)
    records = book.get("sources", [])
    while pending:
        supplied = pending.pop(0)
        handle = aliases.get(supplied, supplied)
        if handle in selected:
            continue
        if handle not in identities:
            raise CandidateError("unknown_reread_source_handle:" + supplied)
        selected.add(handle)
        for record in records:
            if aliases.get(record.get("source_handle"), record.get("source_handle")) == handle:
                pending.extend(_handles(record))
    return [deepcopy(row) for row in records if aliases.get(row.get("source_handle"), row.get("source_handle")) in selected]


def _cited_handles(book, text):
    aliases = book.get("source_aliases", {})
    return {aliases.get(handle, handle) for handle in re.findall(r"\[([^\[\]\n]+)\]", text)
            if aliases.get(handle, handle) in book.get("source_identities", {})}


def _author_payload(book: Mapping[str, Any], ids: Sequence[str], route: str, segments: list[dict[str, Any]],
                    config: Mapping[str, Any], reread_ids: Sequence[str] = (), source_handles: Sequence[str] = (), partial_prefix: str = "") -> dict[str, Any]:
    payload = project_fullbody(book, list(ids))
    # Global provenance belongs to disk, not the model or semantic cache key.
    # Unused source-file hash changes must not repurchase an unchanged window.
    payload.pop("input_manifest", None)
    payload.pop("book_sha256", None)
    catalog = fullbody_task_catalog(book)
    completed = [key for row in segments for key in row["task_ids"]]
    recent = segments if route != "workbench" else segments[-config["recent_prose_segments"]:]
    payload.update(accepted_body_markdown=_join(recent),
                   current_scope_partial_body_markdown=partial_prefix,
                   output_continuation=bool(partial_prefix),
                   continuation_join="exact_append_without_inserted_separator" if partial_prefix else None,
                   accepted_prose_scope="entire_actual_prefix" if route != "workbench" else "recent_exact_segments",
                   manuscript_navigation=_navigation(segments),
                   recent_prose_segments=_navigation(recent),
                   reread_segments=[({**_navigation([row])[0], "full_text_location": "accepted_body_markdown"}
                       if row in recent else deepcopy(row)) for row in segments if row["segment_id"] in reread_ids],
                   writing_position={"completed_task_ids": completed, "current_task_ids": list(ids),
                      "remaining_task_ids": [key for key in catalog if key not in completed and key not in ids],
                      "window_reason": "complete_body" if route == "whole_author" else "approved_chapter_or_complete_task_boundary",
                      "is_final_window": set(completed) | set(ids) == set(catalog)},
                   original_requirements_immutable=True, preceding_prose_is_scientific_evidence=False,
                   navigation_is_scientific_evidence=False)
    if route == "workbench":
        identities = book.get("source_identities", {})
        aliases = book.get("source_aliases", {})
        retrieved = _full_source_records(book, source_handles)
        selected = {aliases.get(row["source_handle"], row["source_handle"]) for row in retrieved}
        payload["source_navigation"] = deepcopy(identities)
        payload["source_alias_navigation"] = deepcopy(aliases)
        payload["reread_sources"] = [{"source_handle": row["source_handle"],
                                      "canonical_source_handle": aliases.get(row["source_handle"], row["source_handle"]), "record_sha256": _hash(row),
                                      "full_record_location": "sources"} for row in retrieved]
        present = {_hash(row) for row in payload["sources"]}
        payload["sources"].extend(deepcopy(row) for row in retrieved if _hash(row) not in present)
        payload["source_identities"].update({key: deepcopy(identities[key]) for key in selected})
        payload["source_aliases"].update({key: value for key, value in aliases.items() if value in selected})
    return payload


def _author_stage(book: Mapping[str, Any], ids: Sequence[str], route: str, segments: list[dict[str, Any]],
                  config: Mapping[str, Any], counter: Any, index: int, reads: Sequence[str] = (), read_index: int = 0,
                  source_handles: Sequence[str] = (), partial_prefix: str = "", continuation_index: int = 0):
    payload = _author_payload(book, ids, route, segments, config, reads, source_handles, partial_prefix)
    stage_id = "author_whole" if route == "whole_author" else f"author_{index:03d}" + (f"_reread_{read_index:02d}" if read_index else "")
    if continuation_index:
        stage_id += f"_continuation_{continuation_index:02d}"
    stage = _stage(stage_id, "writer", _messages(_prompt("writer", route), payload), config["writer"], config, counter,
                   task_ids=list(ids), full_prefix_sha256=_text_hash(_join(segments)),
                   original_outline_sha256=_hash(payload.get("shared_fullbody_context")),
                   material_manifest={"preserved": True, "source_records": [{"sha256": _hash(source),
                      "source_handle": source.get("source_handle")} for source in payload.get("sources", [])]},
                   reread_segment_ids=list(reads), reread_source_handles=list(source_handles))
    if not stage["estimate"]["fits"]:
        stage["required_action"] = ("The complete BODY and evidence do not fit this whole-author profile. Explicitly select continuous_author or workbench, or increase input capacity. No content was shortened and no route was switched."
            if route == "whole_author" else "The complete current task, immutable original outline and required prose/evidence exceed capacity. Increase context capacity" +
            (" or explicitly select workbench to avoid resending remote prose." if route == "continuous_author" else "; requested full prose and original requirements were not discarded."))
    required_output = config.get("whole_body_output_tokens")
    if route == "whole_author" and required_output and required_output > config["writer"]["max_output_tokens"]:
        stage.update(status="capacity_blocked", output_capacity_blocked=True,
                     required_action="The explicit full-BODY output requirement exceeds the configured visible-output allowance. Increase max_output_tokens within provider limits or explicitly select a continuation route.")
    return stage


def _choose_author_stage(book, route, segments, config, counter, index):
    catalog = fullbody_task_catalog(book)
    completed = {key for row in segments for key in row["task_ids"]}
    remaining = [key for key in catalog if key not in completed]
    if route == "whole_author":
        return _author_stage(book, remaining, route, segments, config, counter, index)
    # Intentional chapter boundary is a quality choice, even if whole BODY fits.
    chapter_id = catalog[remaining[0]]["chapter_id"]
    candidates = [key for key in remaining if catalog[key]["chapter_id"] == chapter_id]
    if config.get("max_tasks_per_window"):
        candidates = candidates[:config["max_tasks_per_window"]]
    whole = _author_stage(book, candidates, route, segments, config, counter, index)
    if whole["estimate"]["fits"]:
        return whole
    fitting = None
    for end in range(1, len(candidates) + 1):
        proposed = _author_stage(book, candidates[:end], route, segments, config, counter, index)
        if not proposed["estimate"]["fits"]:
            return fitting or proposed
        fitting = proposed
    return fitting or whole


def _plain_payload(book: Mapping[str, Any], ids: Sequence[str], route: str) -> dict[str, Any]:
    """The same lossless material contract, deliberately without prior prose."""
    payload = project_fullbody(book, list(ids))
    payload.pop("input_manifest", None)
    payload.pop("book_sha256", None)
    catalog = fullbody_task_catalog(book)
    payload.update(baseline_route=route, current_task_ids=list(ids),
                   current_chapter_ids=list(dict.fromkeys(catalog[key]["chapter_id"] for key in ids)),
                   draft_scope="entire_approved_body" if route == "plain_whole" else "independent_approved_chapter_or_intact_task_group",
                   preceding_prose_included=False, original_requirements_immutable=True)
    return payload


def _plain_stage(book, ids, route, config, counter, index):
    payload = _plain_payload(book, ids, route)
    stage_id = "plain_whole" if route == "plain_whole" else f"chapter_{index:03d}"
    stage = _stage(stage_id, "writer", _messages(_prompt("plain_writer", route), payload),
                   config["writer"], config, counter, task_ids=list(ids),
                   independent_draft=True, preceding_prose_included=False,
                   original_outline_sha256=_hash(payload.get("shared_fullbody_context")),
                   material_manifest={"preserved": True, "source_records": [{"sha256": _hash(source),
                       "source_handle": source.get("source_handle")} for source in payload.get("sources", [])]})
    if not stage["estimate"]["fits"]:
        stage["required_action"] = (
            "The complete BODY plan and evidence exceed this plain-whole profile's input capacity. Increase input capacity or explicitly choose chapter_concat; no material was shortened or route switched."
            if route == "plain_whole" else
            "The immutable full BODY plan plus one intact task and its complete evidence exceed writer capacity. Increase input capacity; no task or source material was shortened.")
    if route == "plain_whole":
        _full_output_capacity(stage, config, "writer")
    return stage


def _full_output_capacity(stage, config, role):
    required = config.get("whole_body_output_tokens")
    if required and required > config[role]["max_output_tokens"]:
        stage.update(status="capacity_blocked", output_capacity_blocked=True,
                     required_action="The explicit full-BODY output requirement exceeds this " + role +
                     " profile's visible-output allowance. Increase max_output_tokens within provider limits; no abbreviated BODY or windowed substitute will be generated.")


def _choose_plain_stage(book, remaining, config, counter, index):
    """A natural chapter boundary; subdivide only for measured input capacity."""
    catalog = fullbody_task_catalog(book)
    chapter_id = catalog[remaining[0]]["chapter_id"]
    ids = [key for key in remaining if catalog[key]["chapter_id"] == chapter_id]
    whole = _plain_stage(book, ids, "chapter_concat", config, counter, index)
    if whole["estimate"]["fits"]:
        return whole
    fitting = None
    for end in range(1, len(ids) + 1):
        proposed = _plain_stage(book, ids[:end], "chapter_concat", config, counter, index)
        if not proposed["estimate"]["fits"]:
            return fitting or proposed
        fitting = proposed
    return fitting or whole


def _integration_stage(book, draft, config, counter):
    ids = list(fullbody_task_catalog(book))
    payload = _plain_payload(book, ids, "hierarchical_full")
    payload.update(draft_scope="entire_approved_body", original_body_markdown=draft["body_markdown"],
                   original_body_sha256=_text_hash(draft["body_markdown"]),
                   editing_scope="entire_original_full_body", output_scope="complete_replacement_body",
                   original_draft_is_scientific_evidence=False)
    stage = _stage("integrate_full_body", "reviser", _messages(_prompt("plain_writer", "hierarchical_full"), payload),
                   config["reviser"], config, counter, task_ids=ids,
                   full_original_body_sha256=_text_hash(draft["body_markdown"]),
                   full_body_replacement=True,
                   material_manifest={"preserved": True, "source_records": [{"sha256": _hash(source),
                       "source_handle": source.get("source_handle")} for source in payload.get("sources", [])]},
                   required_action="The complete assembled BODY, immutable full plan and complete evidence must fit one reviser request. Increase reviser context/input capacity or explicitly select a suitable editor profile and reuse INDEPENDENT_FULL_BODY_RESULT.json. The complete independent draft is preserved; no windowed editing is represented as full integration.")
    _full_output_capacity(stage, config, "reviser")
    return stage


def _independent_base_validate(base, book, input_hash):
    if not isinstance(base, Mapping) or (base.get("effective_route") or base.get("requested_route")) != "chapter_concat":
        raise CandidateError("hierarchical_full_requires_complete_chapter_concat_draft")
    try:
        checked = _base_validate(base, book, input_hash)
    except CandidateError as exc:
        raise CandidateError(str(exc).replace("reader_revision", "hierarchical_full")) from exc
    expected = [chapter["chapter_id"] for chapter in book["chapters"]]
    if checked.get("approved_chapter_order") != expected:
        raise CandidateError("hierarchical_full_draft_chapter_order_mismatch")
    catalog = fullbody_task_catalog(book)
    segments = checked.get("segments", [])
    covered = [key for segment in segments for key in segment.get("task_ids", [])]
    if not segments or covered != list(catalog):
        raise CandidateError("hierarchical_full_draft_independent_segment_coverage_mismatch")
    return checked


def _plain_segment(parsed, scope, catalog, outcome, output, index):
    content = parsed["body_markdown"]
    segment_id = f"segment_{index:04d}"
    path = output / "manuscript" / _text_hash(content) / (segment_id + ".md")
    _write(path, content, text=True)
    return {"segment_id": segment_id, "task_ids": list(scope),
            "chapter_ids": list(dict.fromkeys(catalog[key]["chapter_id"] for key in scope)),
            "body_markdown": content, "sha256": _text_hash(content), "full_text_path": str(path),
            "stage_id": outcome["stage_id"], "result_sha256": outcome["result_sha256"],
            "task_dispositions": deepcopy(parsed.get("task_dispositions", []))}


def _independent_draft(book, body, segments, stages, *, output, input_hash, input_path, run_id, execution_mode):
    """A self-contained complete draft, saved before attempting any global edit."""
    ids = list(fullbody_task_catalog(book))
    checked = parse_fullbody_response({"body_markdown": body, "completed_task_ids": ids, "complete": True}, book)
    if not checked["complete"]:
        raise CandidateError("independent_fullbody_draft_structural_validation_failed")
    identity_map = {"source_identities": book.get("source_identities", {}), "source_aliases": book.get("source_aliases", {})}
    identity_path = output / "inputs" / input_hash / "SOURCE_IDENTITY_MAP.json"
    _write(identity_path, identity_map)
    paid = None if any(row.get("paid_dispatch_count") is None for row in stages) else sum(row.get("paid_dispatch_count", 0) for row in stages)
    calls = sum(row.get("model_calls", 0) for row in stages)
    return {**checked, "schema_version": SCHEMA_VERSION + ".result", "body_complete": True,
            "input_hash": input_hash, "input_path": str(input_path), "body_sha256": _text_hash(body),
            "source_identity_map_path": str(identity_path), "source_identity_map_sha256": _hash(identity_map),
            "approved_chapter_order": [chapter["chapter_id"] for chapter in book["chapters"]],
            "fullbody_input_manifest_sha256": _hash(book.get("input_manifest", {})),
            "segments": deepcopy(segments), "task_dispositions": [deepcopy(disposition) for segment in segments
                for disposition in segment.get("task_dispositions", [])],
            "requested_route": "chapter_concat", "effective_route": "chapter_concat", "status": "complete",
            "run_id": run_id, "output_dir": str(output), "execution_mode": execution_mode,
            "selected_kind": "independent_chapter_concatenation", "global_integration_performed": False,
            "assembly_method": "approved_chapter_order_exact_text_join", "material_preserved": True,
            "semantic_quality_unreviewed": True, "model_calls": calls, "client_invocations": calls,
            "paid_dispatch_count": paid, "cost_summary": _cost_summary(stages, None),
            "stage_lineage": [_dependency(row) | {"role": row["role"], "status": row["status"]} for row in stages]}


def _dependency(outcome: Mapping[str, Any]) -> dict[str, Any]:
    return {key: outcome.get(key) for key in ("stage_id", "cache_key", "result_sha256")}


def _base_validate(base: Any, book: Mapping[str, Any], input_hash: str) -> dict[str, Any]:
    if not isinstance(base, Mapping):
        raise CandidateError("reader_revision_requires_complete_base_result")
    if not base.get("complete") or base.get("pending_task_ids") or not isinstance(base.get("body_markdown"), str) or not base["body_markdown"].strip():
        raise CandidateError("reader_revision_requires_complete_full_body")
    if base.get("input_hash") != input_hash:
        raise CandidateError("reader_revision_base_input_hash_mismatch")
    catalog = fullbody_task_catalog(book)
    if base.get("body_sha256") and base["body_sha256"] != _text_hash(base["body_markdown"]):
        raise CandidateError("reader_revision_base_body_hash_mismatch")
    segments = base.get("segments", [])
    if segments and (not isinstance(segments, list) or _join(segments) != base["body_markdown"] or
                     any(row.get("sha256") != _text_hash(row.get("body_markdown", "")) for row in segments)):
        raise CandidateError("reader_revision_base_segment_text_or_hash_mismatch")
    if set(base.get("completed_task_ids", [])) != set(catalog):
        raise CandidateError("reader_revision_base_fullbody_coverage_mismatch")
    checked = parse_fullbody_response({"body_markdown": base["body_markdown"], "completed_task_ids": list(catalog), "complete": True}, book)
    if not checked.get("complete"):
        raise CandidateError("reader_revision_base_structural_validation_failed")
    return deepcopy(dict(base))


def _issue_task_ids(issue: Mapping[str, Any], base: Mapping[str, Any], book: Mapping[str, Any]) -> list[str]:
    catalog = fullbody_task_catalog(book)
    chosen = set()
    # Model-declared exact text locations may identify a task without requiring
    # one visible paragraph per task. Never treat vague prose labels as offsets.
    for disposition in base.get("task_dispositions", []):
        location = disposition.get("location")
        anchor = location.get("anchor") if isinstance(location, Mapping) else None
        if anchor and base["body_markdown"].count(anchor) == 1 and (issue["anchor"] in anchor or anchor in issue["anchor"]):
            chosen.add(disposition.get("task_id"))
    if not chosen:
        # Actual source handles in the affected text give an evidence-grounded
        # narrow scope; all complete tasks referring to those handles are kept.
        aliases = book.get("source_aliases", {})
        original = base["body_markdown"]
        offset = original.index(issue["anchor"])
        start = original.rfind("\n\n", 0, offset)
        end = original.find("\n\n", offset + len(issue["anchor"]))
        paragraph = original[start + 2 if start >= 0 else 0:end if end >= 0 else len(original)]
        cited = {aliases.get(key, key) for key in re.findall(r"\[([^\[\]\n]+)\]", paragraph)
                 if aliases.get(key, key) in book.get("source_identities", {})}
        if cited:
            chosen.update(key for key, row in catalog.items()
                          if cited.intersection(aliases.get(handle, handle) for handle in _handles(row["task"])))
    if not chosen:
        for segment in base.get("segments", []):
            if issue["anchor"] in segment.get("body_markdown", ""):
                chosen.update(segment.get("task_ids", []))
    # Other actual source identities mentioned by the reader identify context
    # to retrieve, not evidence that the reader's scientific criticism is true.
    aliases = book.get("source_aliases", {})
    references = " ".join(str(issue.get(key, "")) for key in ("problem", "reader_understanding", "suggested_action"))
    referred_handles = {aliases.get(key, key) for key in re.findall(r"\[([^\[\]\n]+)\]", references)
                        if aliases.get(key, key) in book.get("source_identities", {})}
    chosen.update(key for key, row in catalog.items()
                  if referred_handles.intersection(aliases.get(handle, handle) for handle in _handles(row["task"])))
    supplied = issue.get("task_ids", [])
    if supplied and (not isinstance(supplied, list) or any(key not in catalog for key in supplied)):
        raise CandidateError("reader_unknown_task_reference")
    chosen.update(supplied)
    chosen.discard(None)
    # Ambiguous links require the whole approved material pool, never guessing
    # that a short citation alone exhausts the scientific conditions involved.
    return [key for key in catalog if not chosen or key in chosen]


def _revision_stage(book, base, issues, config, counter, index, extra_task_ids=(), evidence_round=0, extra_source_handles=()):
    scope = set(key for issue in issues for key in _issue_task_ids(issue, base, book)) | set(extra_task_ids)
    ids = [key for key in fullbody_task_catalog(book) if key in scope]
    payload = project_fullbody(book, ids)
    payload.pop("input_manifest", None)
    payload.pop("book_sha256", None)
    handles = set(extra_source_handles)
    for issue in issues:
        handles.update(_cited_handles(book, " ".join(str(issue.get(key, "")) for key in
            ("anchor", "problem", "reader_understanding", "suggested_action"))))
    records = _full_source_records(book, list(handles))
    seen_records = {_hash(record) for record in payload["sources"]}
    payload["sources"].extend(record for record in records if _hash(record) not in seen_records)
    selected_handles = {book.get("source_aliases", {}).get(row["source_handle"], row["source_handle"]) for row in payload["sources"]}
    payload["source_identities"].update({key: deepcopy(value) for key, value in book.get("source_identities", {}).items() if key in selected_handles})
    payload["source_aliases"].update({key: value for key, value in book.get("source_aliases", {}).items() if value in selected_handles})
    payload.update(body_markdown=base["body_markdown"], original_body_sha256=_text_hash(base["body_markdown"]),
                   reader_issues=deepcopy(issues), edit_scope="exact_unique_anchors_in_original_full_body",
                   untouched_text_must_be_preserved=True, evidence_expansion_task_ids=list(extra_task_ids),
                   evidence_expansion_source_handles=list(extra_source_handles),
                   evidence_reread_reason=("Earlier proposed exact target locations need additional complete source records. Reassess all proposed changes against the expanded evidence before returning final patches." if extra_task_ids else None))
    stage_id = f"revision_{index:03d}" + (f"_evidence_{evidence_round:02d}" if evidence_round else "")
    return _stage(stage_id, "reviser", _messages(_prompt("revision"), payload),
                  config["reviser"], config, counter, task_ids=ids,
                  issue_ids=[row["issue_id"] for row in issues],
                  required_action="The original full BODY plus this issue's full approved evidence do not fit. Increase reviser capacity; the original draft remains selected.")


def _patch_evidence_expansion(book, base, stage, patches):
    """Determine full approved evidence absent for actual proposed edit targets."""
    payload = json.loads(stage["messages"][-1]["content"])
    supplied_records = {_hash(record) for record in payload["sources"]}
    needed_ids, explicit_handles = set(), set()
    catalog = fullbody_task_catalog(book)
    aliases = book.get("source_aliases", {})
    for patch in patches:
        if not patch["replacement"] or re.sub(r"[\W_]+", "", patch["anchor"]) == re.sub(r"[\W_]+", "", patch["replacement"]):
            continue  # Deletion/format-only changes do not add scientific claims.
        needed_ids.update(_issue_task_ids({"anchor": patch["anchor"]}, base, book))
        explicit_handles.update(_cited_handles(book, patch["anchor"] + " " + patch["replacement"]))
        handles = {aliases.get(handle, handle) for handle in re.findall(r"\[([^\[\]\n]+)\]", patch["replacement"])
                   if aliases.get(handle, handle) in book.get("source_identities", {})}
        if handles:
            needed_ids.update(key for key, row in catalog.items()
                              if handles.intersection(aliases.get(h, h) for h in _handles(row["task"])))
    if not needed_ids and not explicit_handles:
        return {}
    needed = project_fullbody(book, [key for key in catalog if key in needed_ids])
    required_records = needed["sources"] + _full_source_records(book, list(explicit_handles))
    missing_records = [row for row in required_records if _hash(row) not in supplied_records]
    if not missing_records:
        return {}
    return {"task_ids": [key for key in catalog if key in needed_ids and key not in stage["task_ids"]],
            "source_handles": list(dict.fromkeys(aliases.get(row["source_handle"], row["source_handle"]) for row in missing_records))}


def _apply_patches(original: str, patches: Sequence[Mapping[str, Any]]) -> str:
    spans = []
    for patch in patches:
        if original.count(patch["anchor"]) != 1:
            raise CandidateError("patch_original_anchor_nonunique")
        start = original.index(patch["anchor"])
        spans.append((start, start + len(patch["anchor"]), patch["replacement"]))
    ordered = sorted(spans)
    if any(a[1] > b[0] for a, b in zip(ordered, ordered[1:])):
        raise CandidateError("overlapping_revision_anchors_across_batches")
    result = original
    for start, end, replacement in reversed(ordered):
        result = result[:start] + replacement + result[end:]
    return result


def _cost_summary(stages: Sequence[Mapping[str, Any]], base: Mapping[str, Any] | None) -> dict[str, Any]:
    selected_known = 0.0
    unknown = []
    all_attempts = []
    seen = set()
    for stage in stages:
        root = Path(stage["attempt_dir"]).parent if stage.get("attempt_dir") else None
        if root is None or str(root) in seen:
            continue
        seen.add(str(root))
        for attempt in sorted(root.glob("attempt_*")):
            usage_path = attempt / "USAGE.json"
            record = _read(usage_path) if usage_path.exists() else {}
            value = record.get("estimated_actual_cost_cny")
            all_attempts.append({"attempt_dir": str(attempt), "estimated_actual_cost_cny": value,
                                 "usage_known": record.get("usage_known", False)})
            if value is None:
                unknown.append(str(attempt))
            else:
                selected_known += value
    base_cost = deepcopy(base.get("cost_summary", {})) if base else None
    return {"candidate_stage_known_cost_cny": selected_known, "candidate_stage_cost_complete": not unknown,
            "unknown_cost_attempts": unknown, "all_attempts_including_retries": all_attempts,
            "base_cost_attribution": base_cost, "base_reuse_charged_again": False,
            "total_known_cost_including_base_cny": selected_known + (base_cost.get("total_known_cost_including_base_cny", base_cost.get("candidate_stage_known_cost_cny", 0)) if base_cost else 0),
            "total_cost_complete": not unknown and (base_cost.get("total_cost_complete", base_cost.get("candidate_stage_cost_complete", False)) if base_cost else True),
            "accounting_note": "Known costs are provider-usage estimates. Missing usage is unknown, never zero; shared-ledger uncertain reservations remain retained. Reused base cost is attributable but is not dispatched or charged again."}


def run_fullbody_candidate(book: dict[str, Any], *, route: str, output_dir: str | Path,
                           config: Mapping[str, Any], client_factory: Any = None,
                           run: bool = False, retry_failed: bool = False,
                           token_counter: Any = None, continue_incomplete: bool = False, base_result: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Preview or execute an explicit full-BODY route, with exact continuation.

    Preview emits only requests whose actual inputs are available. Later
    continuation and edit requests cannot be honestly precomputed without prose.
    ``base_result`` is mandatory for reader_revision and optional for hierarchical_full.
    A complete chapter_concat draft is reused verbatim without purchasing draft calls.
    """
    output = Path(output_dir).resolve()
    with _output_lock(output):
        return _run(book, route=route, output=output, config=config, client_factory=client_factory,
                    run=run, retry_failed=retry_failed, counter=token_counter, base_result=base_result, continue_incomplete=continue_incomplete)


def _run(book, *, route, output, config, client_factory, run, retry_failed, counter, base_result, continue_incomplete):
    if route not in ROUTES:
        raise CandidateError("unknown_fullbody_route:" + str(route))
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:10]
    run_dir = output / "runs" / run_id
    run_dir.mkdir(parents=True)
    input_hash = _hash(book)
    source_hashes = _source_file_hashes()
    code_hash = _hash(source_hashes)
    input_path = output / "inputs" / input_hash / "FULL_BODY_INPUT.json"
    if not input_path.exists():
        _write(input_path, book)
    manifest = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "input_hash": input_hash,
                "input_path": str(input_path), "requested_route": route, "effective_route": route,
                "source_file_hashes": source_hashes, "code_hash": code_hash, "git_commit": _git_commit(),
                "prompt_file_hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(PROMPT_ROOT.glob("*.md"))},
                "execution_mode": getattr(client_factory, "execution_mode", "injected" if client_factory else "preview"),
                "run": bool(run), "stages": [], "not_executed": [], "status": "planning",
                "semantic_quality_unreviewed": True, "material_preserved": True,
                "hidden_final_integration": False, "automatic_paid_retries": False,
                "provenance": deepcopy(book.get("input_manifest", book.get("provenance", {})))}
    segments, partial, body, completed, patches = [], "", "", [], []
    base, dependencies, catalog = None, [], {}
    revision_complete = False
    integration_complete, independent_draft = False, None
    pending_reader_issues = []

    def checkpoint():
        _write(run_dir / "RUN_MANIFEST.json", manifest)
        _write(output / "RUN_MANIFEST.json", manifest)

    def execute(stage, parser, *, stage_route=None, stage_dependencies=None):
        outcome = _execute_stage(stage, output=output, run_dir=run_dir, route=stage_route or route, code_hash=code_hash,
            input_hash=input_hash, dependencies=deepcopy(dependencies if stage_dependencies is None else stage_dependencies), client_factory=client_factory,
            run=run, retry_failed=retry_failed, config=config, counter=counter, parser=parser)
        manifest["stages"].append(outcome)
        checkpoint()
        return outcome

    checkpoint()
    try:
        config = validate_config(config)
        manifest["config"] = config
        catalog = fullbody_task_catalog(book)
        if not catalog:
            raise CandidateError("fullbody_has_no_tasks")
        if continue_incomplete and retry_failed:
            raise CandidateError("choose_continue_incomplete_or_retry_failed_not_both")
        if continue_incomplete and route in PLAIN_ROUTES:
            raise CandidateError("plain_routes_do_not_support_prefix_continuation:use continuous_author or workbench for prefix continuation")
        if route == "reader_revision":
            base = _base_validate(base_result, book, input_hash)
            body, completed = base["body_markdown"], list(catalog)
            segments = deepcopy(base.get("segments", []))
            manifest["base_result"] = {"run_id": base.get("run_id"), "input_hash": base["input_hash"],
                                       "result_sha256": _hash({k: v for k, v in base.items() if k != "_loaded_from"}),
                                       "loaded_from": base.get("_loaded_from"), "cost_summary": base.get("cost_summary")}
            dependencies.append({"stage_id": "reused_complete_body", "result_sha256": _hash({
                "input_hash": base["input_hash"], "body_markdown": base["body_markdown"],
                "completed_task_ids": base["completed_task_ids"],
                "task_dispositions": base.get("task_dispositions", [])})})
            _write(run_dir / "ORIGINAL_FULL_BODY.md", body, text=True)
            _write(run_dir / "ORIGINAL_FULL_BODY_RESULT.json", base)
            reader_payload = {"research_question": book.get("research_question"),
                              "target_reader": book.get("target_reader", book.get("target_audience", book.get("audience", "the intended scientific review audience"))),
                              "user_request": book.get("user_request", ""), "review_scope": book.get("review_scope", ""),
                              "reader_goals": book.get("reader_goals", []), "language": book.get("language", "zh"),
                              "body_markdown": body, "reading_scope": "entire_original_full_body"}
            reader = _stage("reader_full_body", "reader", _messages(_prompt("reader"), reader_payload),
                config["reader"], config, counter, full_original_body_sha256=_text_hash(body),
                required_action="A fresh reader must receive the entire original BODY. Increase reader context capacity; no partial reading is represented as full review.")
            outcome = execute(reader, _reader_parser(body))
            pending_reader_issues = deepcopy(outcome.get("result", {}).get("pending_reader_issues", []))
            manifest["pending_reader_issues"] = pending_reader_issues
            if outcome["status"] == "complete":
                dependencies.append(_dependency(outcome))
                issues = outcome["result"]["issues"]
                if not issues:
                    revision_complete = not pending_reader_issues
                else:
                    # Batch by complete actionable issue, retain full original
                    # draft and corresponding complete evidence in every request.
                    groups, current = [], []
                    for issue in issues:
                        proposed = _revision_stage(book, base, current + [issue], config, counter, len(groups) + 1)
                        if proposed["estimate"]["fits"]:
                            current.append(issue)
                        else:
                            if current:
                                groups.append(current)
                            current = [issue]
                    if current:
                        groups.append(current)
                    outcomes = []
                    for index, group in enumerate(groups, 1):
                        extra_ids, extra_handles, evidence_round, supported_fallback = [], [], 0, []
                        while True:
                            stage = _revision_stage(book, base, group, config, counter, index, extra_ids, evidence_round, extra_handles)
                            revised = execute(stage, _patch_parser(base["body_markdown"], group))
                            if revised["status"] != "complete":
                                break
                            expansion = _patch_evidence_expansion(book, base, stage, revised["result"]["patches"])
                            if expansion:
                                supported_fallback = [patch for patch in revised["result"]["patches"]
                                    if not _patch_evidence_expansion(book, base, stage, [patch])]
                                revised.update(status="evidence_supplement_required", additional_complete_evidence_task_ids=expansion["task_ids"],
                                               additional_complete_evidence_source_handles=expansion["source_handles"])
                                dependencies.append(_dependency(revised))
                                if evidence_round >= config["max_rereads_per_window"]:
                                    revised["required_action"] = "Increase max_rereads_per_window to provide the additional target evidence before applying these patches; the unsupported changes remain unapplied."
                                    break
                                extra_ids = list(dict.fromkeys(extra_ids + expansion["task_ids"]))
                                extra_handles = list(dict.fromkeys(extra_handles + expansion["source_handles"]))
                                evidence_round += 1
                                continue
                            candidate_patches = patches + revised["result"]["patches"]
                            try:
                                candidate = _apply_patches(base["body_markdown"], candidate_patches)
                                checked = parse_fullbody_response({"body_markdown": candidate,
                                    "completed_task_ids": list(catalog), "complete": True}, book)
                                if not checked.get("complete"):
                                    raise CandidateError("revised_body_failed_structural_checks:" + str(checked.get("issues", [])))
                            except Exception as exc:
                                _reject_outcome(revised, "revision_validation_failed", str(exc))
                            else:
                                patches = candidate_patches
                                body = candidate
                                dependencies.append(_dependency(revised))
                            break
                        if revised["status"] != "complete" and supported_fallback:
                            try:
                                fallback_patches = patches + supported_fallback
                                fallback_body = _apply_patches(base["body_markdown"], fallback_patches)
                                fallback_checked = parse_fullbody_response({"body_markdown": fallback_body,
                                    "completed_task_ids": list(catalog), "complete": True}, book)
                                if fallback_checked.get("complete"):
                                    patches, body = fallback_patches, fallback_body
                                    revised["supported_patches_retained"] = deepcopy(supported_fallback)
                            except CandidateError:
                                pass  # Preserve already accepted nonoverlapping edits.
                        outcomes.append(revised)
                        checkpoint()
                    revision_complete = all(row["status"] == "complete" for row in outcomes) and not pending_reader_issues
            else:
                manifest["not_executed"].append({"role": "reviser", "reason": "requires_successful_complete_body_reader"})
        elif route in PLAIN_ROUTES:
            if base_result is not None and route != "hierarchical_full":
                raise CandidateError("base_result_only_supported_for_reader_revision_or_hierarchical_full")
            manifest.update(baseline=True, prior_actual_prose_used_for_drafting=False,
                            assembly_method="single_full_body_call" if route == "plain_whole" else "approved_chapter_order_exact_text_join",
                            global_integration_requested=route == "hierarchical_full")
            if route == "hierarchical_full" and base_result is not None:
                base = _independent_base_validate(base_result, book, input_hash)
                independent_draft = deepcopy(base)
                body, completed = base["body_markdown"], list(catalog)
                segments = deepcopy(base["segments"])
                manifest["base_result"] = {"run_id": base.get("run_id"), "input_hash": base["input_hash"],
                    "result_sha256": _hash({k: v for k, v in base.items() if k != "_loaded_from"}),
                    "loaded_from": base.get("_loaded_from"), "cost_summary": base.get("cost_summary")}
            else:
                remaining = list(catalog)
                while remaining:
                    if len(manifest["stages"]) >= config["max_author_calls"]:
                        manifest["not_executed"].append({"role": "writer", "reason": "explicit_max_author_calls_reached",
                            "required_action": "Increase max_author_calls to continue from cached independent chapter drafts."})
                        break
                    stage = (_plain_stage(book, remaining, "plain_whole", config, counter, 1) if route == "plain_whole" else
                             _choose_plain_stage(book, remaining, config, counter, len(manifest["stages"]) + 1))
                    scope = stage["task_ids"]
                    # Independently authored chapters do not depend on earlier
                    # responses. Share their cache between concat and hierarchy.
                    outcome = execute(stage, _author_parser(book, scope, allow_reads=False),
                        stage_route="plain_whole" if route == "plain_whole" else "chapter_concat", stage_dependencies=[])
                    parsed = outcome.get("result", {})
                    remaining = [key for key in remaining if key not in scope]
                    if outcome["status"] == "planned":
                        continue  # All independent requests can be previewed honestly.
                    if outcome["status"] != "complete":
                        partial = parsed.get("body_markdown", "")
                        break  # Keep useful prose and cache; never invent missing chapters.
                    segments.append(_plain_segment(parsed, scope, catalog, outcome, output, len(segments) + 1))
                    completed.extend(scope)
                    body = _join(segments)
                    _write(run_dir / "PARTIAL_BODY.md", body, text=True)
                    _write(run_dir / "MANUSCRIPT_SEGMENTS.json", segments)
                    checkpoint()
                if route != "plain_whole" and completed == list(catalog):
                    independent_draft = _independent_draft(book, body, segments, manifest["stages"], output=output,
                        input_hash=input_hash, input_path=input_path, run_id=run_id, execution_mode=manifest["execution_mode"])
            if independent_draft is not None:
                # Immutable per-run copy remains usable when a later editor
                # errors, is interrupted, or changes its configured model.
                for directory in (run_dir, output):
                    _write(directory / "INDEPENDENT_FULL_BODY_RESULT.json", independent_draft)
                    _write(directory / "INDEPENDENT_FULL_BODY.md", independent_draft["body_markdown"], text=True)
                manifest["independent_draft_result_path"] = str(run_dir / "INDEPENDENT_FULL_BODY_RESULT.json")
                manifest["independent_draft_body_path"] = str(run_dir / "INDEPENDENT_FULL_BODY.md")
                checkpoint()
            if route == "hierarchical_full":
                if independent_draft is None:
                    manifest["not_executed"].append({"role": "reviser", "reason": "requires_complete_independent_full_body_draft",
                        "required_action": "Complete every approved chapter before requesting full-BODY integration."})
                else:
                    _write(run_dir / "ORIGINAL_FULL_BODY.md", body, text=True)
                    _write(run_dir / "ORIGINAL_FULL_BODY_RESULT.json", independent_draft)
                    stage = _integration_stage(book, independent_draft, config, counter)
                    draft_dependency = {"stage_id": "complete_independent_body", "result_sha256": _hash({
                        "input_hash": input_hash, "body_markdown": body,
                        "completed_task_ids": independent_draft["completed_task_ids"],
                        "task_dispositions": independent_draft.get("task_dispositions", [])})}
                    outcome = execute(stage, _author_parser(book, list(catalog), allow_reads=False), stage_dependencies=[draft_dependency])
                    if outcome["status"] == "complete":
                        parsed = outcome["result"]
                        replacement_segments = [_plain_segment(parsed, list(catalog), catalog, outcome, output, 1)]
                        body, segments = parsed["body_markdown"], replacement_segments
                        integration_complete = True
                    else:
                        actual_capacity_blocked = "actual_client_meter_requires_batching" in str(outcome.get("call_error") or "")
                        outcome["required_action"] = stage.get("required_action") if outcome["status"] == "capacity_blocked" or actual_capacity_blocked else (
                            "The full-BODY edit was not accepted. The complete independent draft is preserved. Inspect the saved raw response and shared ledger; use --retry-failed only to explicitly authorize another attempt, or reuse INDEPENDENT_FULL_BODY_RESULT.json with a suitable editor profile.")
                        checkpoint()
        else:
            while len(completed) < len(catalog):
                if len(manifest["stages"]) >= config["max_author_calls"]:
                    manifest["not_executed"].append({"role": "writer", "reason": "explicit_max_author_calls_reached", "required_action": "Increase max_author_calls to continue from cached accepted prose."})
                    break
                stage = _choose_author_stage(book, route, segments, config, counter, len(segments) + 1)
                scope = stage["task_ids"]
                read_ids, read_sources, read_count, continuation_index, scope_prefix = [], [], 0, 0, ""
                while True:
                    outcome = execute(stage, _author_parser(book, scope, allow_reads=route == "workbench", prefix=scope_prefix))
                    parsed = outcome.get("result", {})
                    if outcome["status"] != "complete":
                        tail = parsed.get("body_markdown", "")
                        partial = scope_prefix + tail
                        can_continue = (continue_incomplete and route != "whole_author" and tail and
                            parsed.get("finish_reason") in ("length", "max_tokens", "max_output_tokens") and
                            (outcome.get("usage") or {}).get("estimated_actual_cost_cny") is not None and not parsed.get("call_error"))
                        if can_continue and len(manifest["stages"]) < config["max_author_calls"]:
                            dependencies.append(_dependency(outcome))
                            scope_prefix = partial
                            continuation_index += 1
                            stage = _author_stage(book, scope, route, segments, config, counter, len(segments) + 1,
                                read_ids, read_count, read_sources, scope_prefix, continuation_index)
                            continue
                        break
                    dependencies.append(_dependency(outcome))
                    if parsed.get("kind") != "reread_request":
                        break
                    requested = parsed["read_segment_ids"]
                    requested_sources = parsed.get("read_source_handles", [])
                    known = {row["segment_id"] for row in segments}
                    if any(key not in known for key in requested):
                        _reject_outcome(outcome, "invalid_reread_request", "Author requested an unknown manuscript segment; inspect the saved request and explicitly retry with corrected protocol.")
                        break
                    if any(book.get("source_aliases", {}).get(key, key) not in book.get("source_identities", {}) for key in requested_sources):
                        _reject_outcome(outcome, "invalid_reread_request", "Author requested an unknown source identity; inspect the saved source navigation. No alternate identity was substituted.")
                        break
                    actual_payload = json.loads(stage["messages"][-1]["content"])
                    visible_segments = {row["segment_id"] for row in actual_payload.get("recent_prose_segments", []) + actual_payload.get("reread_segments", [])}
                    visible_sources = {book.get("source_aliases", {}).get(row["source_handle"], row["source_handle"]) for row in actual_payload.get("sources", [])}
                    canonical_sources = list(dict.fromkeys(book.get("source_aliases", {}).get(key, key) for key in requested_sources))
                    fresh = [key for key in requested if key not in visible_segments]
                    fresh_sources = [key for key in canonical_sources if key not in visible_sources]
                    outcome["resolved_read_source_handles"] = canonical_sources
                    if not fresh and not fresh_sources:
                        _reject_outcome(outcome, "reread_no_progress", "All requested full prose or canonical source records were already provided; inspect the repeated request. No automatic paid loop was started.")
                        break
                    read_count += 1
                    if read_count > config["max_rereads_per_window"] or len(manifest["stages"]) >= config["max_author_calls"]:
                        outcome.update(status="reread_limit_reached", required_action="Increase the explicit reread/call bound to continue; every requested and delivered segment remains recorded.")
                        break
                    read_ids.extend(fresh)
                    read_sources.extend(fresh_sources)
                    stage = _author_stage(book, scope, route, segments, config, counter, len(segments) + 1,
                        read_ids, read_count, read_sources, scope_prefix, continuation_index)
                if outcome["status"] != "complete" or parsed.get("kind") == "reread_request":
                    if not run and outcome["status"] == "planned":
                        manifest["not_executed"].append({"role": "writer", "reason": "later_requests_require_actual_accepted_prose", "remaining_task_ids": [key for key in catalog if key not in completed]})
                    break
                segment_id = f"segment_{len(segments) + 1:04d}"
                content = scope_prefix + parsed["body_markdown"]
                partial = ""
                segment = {"segment_id": segment_id, "task_ids": list(scope),
                           "chapter_ids": list(dict.fromkeys(catalog[key]["chapter_id"] for key in scope)),
                           "body_markdown": content, "sha256": _text_hash(content),
                           "full_text_path": str(output / "manuscript" / _text_hash(content) / (segment_id + ".md")),
                           "stage_id": outcome["stage_id"], "result_sha256": outcome["result_sha256"],
                           "task_dispositions": deepcopy(parsed.get("task_dispositions", []))}
                _write(Path(segment["full_text_path"]), content, text=True)
                segments.append(segment)
                completed.extend(scope)
                body = _join(segments)
                _write(run_dir / "PARTIAL_BODY.md", body, text=True)
                _write(run_dir / "MANUSCRIPT_SEGMENTS.json", segments)
                checkpoint()
                if route == "whole_author":
                    break
        if patches:
            original_segments_path = run_dir / "ORIGINAL_MANUSCRIPT_SEGMENTS.json"
            _write(original_segments_path, segments)
            segment_id = "revised_full_body"
            segment_path = output / "manuscript" / _text_hash(body) / (segment_id + ".md")
            _write(segment_path, body, text=True)
            segments = [{"segment_id": segment_id, "task_ids": list(catalog),
                "chapter_ids": list(dict.fromkeys(row["chapter_id"] for row in catalog.values())),
                "body_markdown": body, "sha256": _text_hash(body), "full_text_path": str(segment_path),
                "stage_id": "reader_revised_body", "result_sha256": _hash({"body": body, "patches": patches})}]
            manifest["original_manuscript_segments_path"] = str(original_segments_path)
        if partial:
            body = body + ("\n\n" if body else "") + partial
        checked = parse_fullbody_response({"body_markdown": body, "completed_task_ids": completed,
                                            "complete": len(completed) == len(catalog)}, book)
        if base and patches:
            for disposition in base.get("task_dispositions", []):
                location = disposition.get("location")
                anchor = location.get("anchor") if isinstance(location, Mapping) else None
                if anchor and body.count(anchor) != 1:
                    disposition["original_location"] = disposition.pop("location")
                    disposition["location_invalidated_by_revision"] = True
        complete = bool(checked.get("complete") and (route != "reader_revision" or revision_complete) and
                        (route != "hierarchical_full" or integration_complete))
        manifest["status"] = "complete" if complete else "preview" if not run else "pending"
        result = {**checked, "complete": complete, "body_complete": bool(checked.get("complete")), "body_markdown": body,
                  "pending_task_ids": [key for key in catalog if key not in completed],
                  "completed_task_ids": completed, "segments": segments, "accepted_patches": patches,
                  "task_dispositions": deepcopy(base.get("task_dispositions", [])) if base and not integration_complete else
                      [deepcopy(disposition) for segment in segments for disposition in segment.get("task_dispositions", [])],
                  "selected_kind": "reader_revised" if patches else "original_base" if base else "draft",
                  "reader_revision_complete": revision_complete if route == "reader_revision" else None,
                  "reader_revision_status": ("complete" if revision_complete else "partial" if patches else "pending") if route == "reader_revision" else None,
                  "pending_reader_issues": pending_reader_issues,
                  "partial_unaccepted_prose": bool(partial)}
        if route in PLAIN_ROUTES:
            result.update(baseline=True, global_integration_performed=integration_complete,
                assembly_method="single_full_body_replacement_of_independent_draft" if integration_complete else manifest["assembly_method"],
                selected_kind=("full_body_integrated" if integration_complete else "independent_chapter_concatenation"
                    if route != "plain_whole" else "plain_whole_draft"),
                independent_draft_result_path=manifest.get("independent_draft_result_path"),
                independent_draft_body_path=manifest.get("independent_draft_body_path"))
            if route == "hierarchical_full":
                result.update(integration_complete=integration_complete, integration_pending=not integration_complete,
                    integration_status="complete" if integration_complete else "pending" if independent_draft else "awaiting_complete_draft")
                if not integration_complete:
                    last = manifest["stages"][-1] if manifest["stages"] else {}
                    result["required_action"] = last.get("required_action") or "Complete every approved chapter before full-BODY integration; no missing chapter is inferred."
    except Exception as exc:
        manifest.update(status="blocked", error=_safe_error(exc))
        if route == "hierarchical_full" and independent_draft is not None:
            body = independent_draft["body_markdown"]
            segments = deepcopy(independent_draft["segments"])
            completed = list(independent_draft["completed_task_ids"])
        result = {"complete": False, "body_markdown": body, "completed_task_ids": completed,
                  "pending_task_ids": [key for key in catalog if key not in completed],
                  "segments": segments, "issues": [manifest["error"]], "accepted_patches": patches,
                  "pending_reader_issues": pending_reader_issues}
        if route == "hierarchical_full":
            result.update(body_complete=bool(independent_draft), integration_complete=False, integration_pending=True,
                          integration_status="pending" if independent_draft else "awaiting_complete_draft",
                          independent_draft_result_path=manifest.get("independent_draft_result_path"),
                          independent_draft_body_path=manifest.get("independent_draft_body_path"),
                          required_action="Inspect the recorded failure before retrying. The complete independent draft, when available, remains reusable for one full-BODY editor call.")
    calls = sum(row.get("model_calls", 0) for row in manifest["stages"])
    paid = None if any(row.get("paid_dispatch_count") is None for row in manifest["stages"]) else sum(row.get("paid_dispatch_count", 0) for row in manifest["stages"])
    cost = _cost_summary(manifest["stages"], base)
    manifest.update(model_calls=calls, client_invocations=calls, paid_dispatch_count=paid, cost_summary=cost)
    identity_path = output / "inputs" / input_hash / "SOURCE_IDENTITY_MAP.json"
    identity_map = {"source_identities": book.get("source_identities", {}), "source_aliases": book.get("source_aliases", {})}
    _write(identity_path, identity_map)
    result.update(schema_version=SCHEMA_VERSION + ".result", input_hash=input_hash, input_path=str(input_path),
                  source_identity_map_path=str(identity_path), source_identity_map_sha256=_hash(identity_map),
                  approved_chapter_order=[chapter["chapter_id"] for chapter in book.get("chapters", [])],
                  fullbody_input_manifest_sha256=_hash(book.get("input_manifest", {})),
                  body_sha256=_text_hash(result["body_markdown"]), run_id=run_id,
                  requested_route=route, effective_route=route, status=manifest["status"],
                  semantic_quality_unreviewed=True, material_preserved=True, output_dir=str(output),
                  execution_mode=manifest["execution_mode"], model_calls=calls, client_invocations=calls,
                  paid_dispatch_count=paid, cost_summary=cost,
                  stage_lineage=[_dependency(row) | {"role": row["role"], "status": row["status"]} for row in manifest["stages"]])
    _write(run_dir / "FULL_BODY_RESULT.json", result)
    _write(run_dir / "FULL_BODY.md", result["body_markdown"], text=True)
    selected = result
    existing = output / "FULL_BODY_RESULT.json"
    if existing.exists():
        try:
            previous = _read(existing)
            if previous.get("complete") and not result["complete"]:
                selected = previous
                manifest["previous_complete_version_preserved"] = True
        except (OSError, ValueError):
            pass
    manifest.update(selected_version=selected["run_id"], current_run_version=run_id,
                    selected_input_matches_current=selected.get("input_hash") == input_hash,
                    selected_result_path=str(output / "runs" / selected["run_id"] / "FULL_BODY_RESULT.json"))
    if selected is result:
        _write(existing, result)
        _write(output / "FULL_BODY.md", result["body_markdown"], text=True)
    checkpoint()
    public_stages = [{k: v for k, v in row.items() if k != "result"} for row in manifest["stages"]]
    report = {**{key: value for key, value in manifest.items() if key != "stages"}, "stages": public_stages,
              "verification_scope": "Complete BODY task coverage, exact source-preserving requests, transport, exact-anchor patching and execution accounting; scientific and editorial quality remain unreviewed."}
    _write(run_dir / "IMPLEMENTATION_REPORT.json", report)
    _write(output / "IMPLEMENTATION_REPORT.json", report)
    return {**result, "stages": public_stages, "selected_version": manifest["selected_version"],
            "selected_result_path": manifest["selected_result_path"],
            "selected_input_matches_current": manifest["selected_input_matches_current"]}
