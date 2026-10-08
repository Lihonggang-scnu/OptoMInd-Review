"""Guide-first chapter authoring with exact-prefix requests and auditable recovery.

Only transport, bounded source reads and declared content gaps are coordinated.
No semantic critic, legacy task completion contract or hidden editor is used.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import re
from pathlib import Path
import time
from typing import Any, Mapping
import uuid

from .fullbody_writer import (_cost_summary, _dependency, _execute_stage, _messages,
                              _stage, _text_hash)
from .writer_candidates import ROOT, _git_commit, _hash, _output_lock, _profile, _read, _safe_error, _write
from .writer_candidates_contracts import CandidateError
from .guided_body_contracts import (validate_guide, compile_guided_materials, build_author_payload,
    resolve_read_request, parse_guided_response, parse_completion_response, apply_insertions, normalize_citation_handles)

SCHEMA_VERSION = "optomind.guided_body_writer.v1"
PROMPT_ROOT = ROOT / "prompts" / "guided_body_writer"


def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        raise CandidateError("config_must_be_object")
    allowed = {"schema_version", "purpose", "writer", "max_input_tokens", "max_rereads_per_chapter",
               "max_author_calls", "completion_on_missing"}
    if set(config) - allowed:
        raise CandidateError("unknown_config_keys:" + ",".join(sorted(set(config) - allowed)))
    result = deepcopy(dict(config))
    if not isinstance(config.get("writer", {}), Mapping):
        raise CandidateError("profile_must_be_object:writer")
    result["writer"] = _profile(config.get("writer", {}), "writer")
    for key, default in (("max_input_tokens", None), ("max_rereads_per_chapter", 3), ("max_author_calls", 64)):
        value = config.get(key, default)
        if value is None and key == "max_input_tokens":
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < (0 if key == "max_rereads_per_chapter" else 1):
            raise CandidateError(key + "_must_be_" + ("nonnegative" if key == "max_rereads_per_chapter" else "positive") + "_integer")
        result[key] = value
    result["completion_on_missing"] = config.get("completion_on_missing", True)
    if not isinstance(result["completion_on_missing"], bool):
        raise CandidateError("completion_on_missing_must_be_boolean")
    return result


def _source_file_hashes():
    paths = [Path(__file__), Path(__file__).with_name("guided_body_contracts.py"),
        Path(__file__).with_name("fullbody_writer.py"), Path(__file__).with_name("fullbody_contracts.py"),
        Path(__file__).with_name("writing_evidence.py"), Path(__file__).with_name("writer_candidates.py"),
        Path(__file__).with_name("writer_candidates_contracts.py"), Path(__file__).parent / "module4/runtime.py"]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def _author_parser(response):
    parsed = parse_guided_response(response)
    # A successfully delivered declared gap is reusable author work. Completion
    # belongs to the chapter, independently of this transport/cache operation.
    parsed["chapter_complete"] = parsed.get("complete", False)
    parsed["complete"] = bool(parsed.get("kind") == "reread_request" or
                               (parsed.get("body_markdown") and not parsed.get("errors")))
    return parsed


def _completion_parser(response, original):
    parsed = parse_completion_response(response, original)
    parsed["chapter_complete"] = parsed.get("complete", False)
    parsed["complete"] = bool(parsed.get("body_markdown") and not parsed.get("errors"))
    return parsed


_METADATA_ERRORS = {"guided_metadata_missing", "guided_metadata_incomplete_or_invalid",
                    "guided_complete_missing_or_invalid", "guided_remaining_content_invalid",
                    "guided_complete_with_remaining_content"}


def _metadata_unresolved(parsed):
    errors = set(parsed.get("errors", []))
    return bool(parsed.get("transport_complete") and parsed.get("body_markdown") and
                errors and errors <= _METADATA_ERRORS)


def _recover_metadata(outcome, declaration):
    """Human metadata only, bound to this request and immutable saved response."""
    expected = {"stage_id", "cache_key", "raw_response_sha256", "body_sha256", "complete", "remaining_content"}
    if not isinstance(declaration, Mapping) or set(declaration) != expected:
        raise CandidateError("guided_metadata_declaration_fields_invalid")
    parsed = outcome.get("result", {})
    if not _metadata_unresolved(parsed) or parsed.get("call_error"):
        raise CandidateError("guided_metadata_recovery_requires_delivered_body_with_metadata_error")
    path = Path(outcome["attempt_dir"]) / "RAW_RESPONSE.json"
    checks = {"stage_id": outcome["stage_id"], "cache_key": outcome["cache_key"],
              "raw_response_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "body_sha256": _text_hash(parsed["body_markdown"])}
    for key, value in checks.items():
        if declaration[key] != value:
            raise CandidateError("guided_metadata_declaration_mismatch:" + key)
    recovered = _author_parser({"body_markdown": parsed["body_markdown"],
        "complete": declaration["complete"], "remaining_content": declaration["remaining_content"]})
    if recovered.get("errors"):
        raise CandidateError("guided_metadata_declaration_invalid")
    recovered["metadata_recovery"] = {"source": "explicit_human_declaration", **deepcopy(dict(declaration))}
    return {**outcome, "result": recovered, "result_sha256": _hash(recovered), "status": "complete"}


def run_guided_body(book, guide, output_dir, config, client_factory=None, run=False,
                    retry_failed=False, token_counter=None, counter=None, metadata_declarations=None):
    """Preview or run the frozen guide, retaining full evidence and actual prose.

    Preview stops at the first unknown author output. An oversized chapter is
    blocked intact; this route never invents subdivisions or shortens materials.
    """
    output = Path(output_dir).resolve()
    with _output_lock(output):
        return _run(book, guide, output, config, client_factory, run, retry_failed,
                    token_counter if token_counter is not None else counter, metadata_declarations)


def _run(book, guide, output, config, client_factory, run, retry_failed, counter, metadata_declarations):
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:10]
    run_dir = output / "runs" / run_id
    run_dir.mkdir(parents=True)
    input_hash, guide_hash = _hash(book), _hash(guide)
    hashes = _source_file_hashes()
    input_path = output / "inputs" / input_hash / "FULL_BODY_INPUT.json"
    guide_path = output / "guides" / guide_hash / "GUIDE.json"
    _write(input_path, book)
    _write(guide_path, guide)
    mode = getattr(client_factory, "execution_mode", "injected" if client_factory else "preview")
    manifest = dict(schema_version=SCHEMA_VERSION, run_id=run_id, input_hash=input_hash,
        input_path=str(input_path), guide_sha256=guide_hash, guide_path=str(guide_path),
        source_file_hashes=hashes, code_hash=_hash(hashes), git_commit=_git_commit(),
        prompt_file_hashes={p.name: _text_hash(p.read_text(encoding="utf-8")) for p in sorted(PROMPT_ROOT.glob("*.md"))},
        requested_route="guided_body", effective_route="guided_body", execution_mode=mode,
        run=bool(run), stages=[], status="planning", material_preserved=True,
        semantic_quality_unreviewed=True, hidden_final_integration=False, automatic_paid_retries=False)
    segments, issues, completed = [], [], []
    dependencies = [{"input_hash": input_hash, "guide_sha256": guide_hash}]
    body, status, action = "", "pending", None
    declarations = [] if metadata_declarations is None else metadata_declarations
    used_declarations = set()
    known_handles = []

    def checkpoint():
        _write(run_dir / "RUN_MANIFEST.json", manifest)
        _write(output / "RUN_MANIFEST.json", manifest)

    def execute(payload, stage_id, parser, chapter_id, completion=False):
        nonlocal status, action
        if len(manifest["stages"]) >= config["max_author_calls"]:
            status, action = "author_call_limit", "Increase max_author_calls to continue the intact guide."
            return None
        prompt = (PROMPT_ROOT / ("completion.md" if completion else "writer.md")).read_text(encoding="utf-8")
        stage = _stage(stage_id, "writer", _messages(prompt, payload), config["writer"], config, counter,
            chapter_id=chapter_id, guide_sha256=guide_hash, full_prefix_sha256=_text_hash(body),
            material_preserved=True)
        if not stage["estimate"]["fits"]:
            stage["required_action"] = ("The whole guide, intact chapter evidence and full actual prefix exceed input capacity. "
                "Increase available context capacity. Natural within-chapter guide subdivision is not implemented in this route; frozen chapter identities cannot be split here. No truncation or legacy task subdivision was performed.")
        declaration = next((row for row in declarations if row["stage_id"] == stage_id), None)
        outcome = _execute_stage(stage, output=output, run_dir=run_dir, route="guided_body",
            code_hash=manifest["code_hash"], input_hash=input_hash, dependencies=deepcopy(dependencies),
            client_factory=client_factory, run=run and declaration is None,
            retry_failed=retry_failed and declaration is None, config=config, counter=counter, parser=parser)
        # The shared transport wrapper predates direct guide JSON and treats
        # every top-level complete:false as a transport failure. A direct body
        # or insertion object instead declares a semantic gap. Reparse only
        # that shape from the already-saved raw response; provider envelopes
        # and recorded transport exceptions retain their original stop state.
        saved = outcome.get("result", {})
        if (saved.get("kind") in ("author", "completion") and not saved.get("transport_complete") and not saved.get("call_error")
                and outcome.get("attempt_dir")):
            raw_path = Path(outcome["attempt_dir"]) / "RAW_RESPONSE.json"
            raw = _read(raw_path) if raw_path.exists() else None
            if isinstance(raw, Mapping) and "content" not in raw and ("body_markdown" in raw or "insertions" in raw):
                reparsed = parser(raw)
                if reparsed.get("transport_complete"):
                    outcome.update(result=reparsed, result_sha256=_hash(reparsed),
                                   status="complete" if reparsed.get("complete") else "pending")
                    _write(Path(outcome["attempt_dir"]) / "RESULT.json", reparsed)
                    _write(Path(outcome["attempt_dir"]) / "BODY.md", reparsed.get("body_markdown", ""), text=True)
        if declaration is not None:
            outcome = _recover_metadata(outcome, declaration)
            used_declarations.add(stage_id)
            _write(run_dir / "metadata_recovery" / (stage_id + ".json"), declaration)
        manifest["stages"].append(outcome)
        checkpoint()
        if "result" not in outcome:
            status = "preview" if outcome["status"] == "planned" else outcome["status"]
            action = outcome.get("required_action", outcome.get("error"))
            return None
        dependencies.append(_dependency(outcome))
        return outcome

    checkpoint()
    try:
        if not isinstance(declarations, list) or any(not isinstance(row, Mapping) or
                not isinstance(row.get("stage_id"), str) for row in declarations):
            raise CandidateError("guided_metadata_declarations_must_be_list")
        if len({row["stage_id"] for row in declarations}) != len(declarations):
            raise CandidateError("guided_metadata_declaration_duplicate_stage")
        config = validate_config(config)
        manifest["config"] = config
        normalized = validate_guide(guide, book)
        # Fail stale/typo declarations before ANY stage can dispatch. Do not
        # migrate old-code or another guide's saved attempt into this run.
        for declaration in declarations:
            stage_id, key = declaration["stage_id"], declaration.get("cache_key", "")
            match = re.fullmatch(r"author_([0-9]{3})(?:_reread_[0-9]{2})?", stage_id)
            if (not match or not 1 <= int(match.group(1)) <= len(normalized["chapters"])
                    or not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key)):
                raise CandidateError("guided_metadata_declaration_stage_invalid")
            attempts = sorted((output / "stages" / stage_id / key).glob("attempt_*"))
            if not attempts:
                raise CandidateError("guided_metadata_declaration_saved_stage_missing")
            attempt = attempts[-1]
            request = _read(attempt / "REQUEST.json")
            signature = request["signature"]
            saved_messages = _read(attempt / "MESSAGES.json")
            current_system = _messages((PROMPT_ROOT / "writer.md").read_text(encoding="utf-8"), {})[0]
            if (signature["code_hash"] != manifest["code_hash"] or
                    signature["dependencies"][0] != dependencies[0] or
                    signature["effective_profile"] != config["writer"] or
                    signature["execution_mode"] != mode or
                    signature.get("recording_fixture_sha256") != getattr(client_factory, "fixture_sha256", None) or
                    saved_messages[0] != current_system):
                raise CandidateError("guided_metadata_declaration_context_changed")
            _recover_metadata({"stage_id": stage_id, "cache_key": key,
                "attempt_dir": str(attempt), "result": _read(attempt / "RESULT.json")}, declaration)
        pack = compile_guided_materials(book)
        known_handles = list(dict.fromkeys([*pack["source_identities"], *pack["source_aliases"]]))
        for index, chapter in enumerate(normalized["chapters"], 1):
            chapter_id = chapter["chapter_id"]
            atoms, reads = [], 0
            outcome = None
            while True:
                payload = build_author_payload(pack, normalized, chapter, body, reread_atoms=atoms)
                outcome = execute(payload, f"author_{index:03d}" + (f"_reread_{reads:02d}" if reads else ""), _author_parser, chapter_id)
                if outcome is None:
                    break
                parsed = outcome["result"]
                if not parsed.get("transport_complete") or parsed.get("kind") != "reread_request":
                    break
                if reads >= config["max_rereads_per_chapter"]:
                    status, action = "reread_limit", "The chapter exhausted its bounded evidence reads."
                    outcome = None
                    break
                requested = resolve_read_request(pack, parsed)
                present = {row["atom_id"] for row in payload["materials"]["evidence_atoms"]}
                fresh = [atom for atom in requested if atom not in present]
                if not fresh:
                    status, action = "repeated_read_request", "The requested evidence has already been supplied."
                    outcome = None
                    break
                atoms.extend(fresh)
                reads += 1
            if outcome is None:
                break
            parsed = outcome["result"]
            draft = parsed.get("body_markdown", "")
            healthy = bool(parsed.get("transport_complete") and not parsed.get("errors") and draft)
            chapter_complete = bool(healthy and parsed.get("chapter_complete") and not parsed.get("remaining_content"))
            remaining = deepcopy(parsed.get("remaining_content", []))
            original = draft
            supplement = None
            if healthy and remaining and config["completion_on_missing"]:
                payload = build_author_payload(pack, normalized, chapter, body, reread_atoms=atoms)
                assignment = payload["chapter_assignment"]
                assignment.update(draft_body_markdown=draft, remaining_content=remaining)
                supplement = execute(payload, f"complete_{index:03d}",
                    lambda response: _completion_parser(response, original), chapter_id, completion=True)
                if supplement is not None:
                    patch = supplement["result"]
                    if patch.get("transport_complete") and patch.get("complete") and not patch.get("errors"):
                        draft = apply_insertions(original, patch.get("insertions", []))
                        remaining = deepcopy(patch.get("remaining_content", []))
                        chapter_complete = bool(patch.get("chapter_complete") and not remaining)
                    else:
                        issues.append({"chapter_id": chapter_id, "code": "completion_pending", "errors": patch.get("errors", patch.get("issues", []))})
                        # A failed transport is a stop, even with a useful draft.
                        healthy = healthy and bool(patch.get("transport_complete"))
            if draft:
                path = output / "manuscript" / _text_hash(draft) / f"chapter_{index:03d}.md"
                _write(path, draft, text=True)
                segments.append(dict(chapter_id=chapter_id, body_markdown=draft, sha256=_text_hash(draft),
                    full_text_path=str(path), complete=chapter_complete, remaining_content=remaining,
                    original_draft_sha256=_text_hash(original), stage_id=outcome["stage_id"]))
                body = "\n\n".join(row["body_markdown"] for row in segments)
            if chapter_complete:
                completed.append(chapter_id)
            if not healthy:
                status = "transport_failed" if not parsed.get("transport_complete") or (supplement and not supplement["result"].get("transport_complete")) else "response_invalid"
                if _metadata_unresolved(parsed):
                    status = "metadata_unresolved"
                    raw_path = Path(outcome["attempt_dir"]) / "RAW_RESPONSE.json"
                    action = {"instruction": "Review this exact saved body and explicitly declare complete and remaining_content; resume with metadata_declarations. No author rewrite is needed.",
                        "declaration_binding": {"stage_id": outcome["stage_id"], "cache_key": outcome["cache_key"],
                            "raw_response_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                            "body_sha256": _text_hash(draft)}}
                issues.extend(parsed.get("errors", parsed.get("issues", [])))
                break
            if remaining and supplement is None and config["completion_on_missing"]:
                break
            # A healthy provisional chapter remains in the exact prefix; pending
            # declared gaps never become a claim of complete manuscript coverage.
        else:
            if {row["stage_id"] for row in declarations} != used_declarations:
                raise CandidateError("guided_metadata_declaration_unused")
            status = "complete" if len(completed) == len(normalized["chapters"]) else "pending"
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
    manifest.update(status=status, model_calls=calls, client_invocations=calls, paid_dispatch_count=paid, cost_summary=cost)
    order = [row["chapter_id"] for row in guide.get("chapters", [])] if isinstance(guide, Mapping) else []
    delivery_body = normalize_citation_handles(body, known_handles)
    delivery_path = run_dir / "DELIVERY_BODY.md"
    _write(delivery_path, delivery_body, text=True)
    result = dict(delivery_body_path=str(delivery_path), delivery_body_sha256=_text_hash(delivery_body),
        delivery_citation_format_only=True,
        unknown_citation_handles=sorted(set(re.findall(r"(?<![A-Za-z0-9_])P[0-9]{4}(?![A-Za-z0-9_])", body)) - set(known_handles)),
        schema_version=SCHEMA_VERSION + ".result", run_id=run_id, complete=complete, body_complete=complete,
        body_markdown=body, body_sha256=_text_hash(body), segments=segments,
        completed_chapter_ids=completed, pending_chapter_ids=[key for key in order if key not in completed],
        approved_chapter_order=order, issues=issues, status=status, required_action=action,
        input_hash=input_hash, input_path=str(input_path), guide_sha256=guide_hash, guide_path=str(guide_path),
        output_dir=str(output), requested_route="guided_body", effective_route="guided_body", execution_mode=mode,
        semantic_quality_unreviewed=True, material_preserved=True, hidden_final_integration=False,
        model_calls=calls, client_invocations=calls, paid_dispatch_count=paid, cost_summary=cost,
        stage_lineage=[_dependency(row) | {"role": row["role"], "status": row["status"]} for row in manifest["stages"]])
    _write(run_dir / "FULL_BODY_RESULT.json", result)
    _write(run_dir / "FULL_BODY.md", body, text=True)
    selected = result
    existing = output / "FULL_BODY_RESULT.json"
    if existing.exists():
        previous = _read(existing)
        if previous.get("complete") and not complete:
            selected = previous
            manifest["previous_complete_version_preserved"] = True
    if selected is result:
        _write(existing, result)
        _write(output / "FULL_BODY.md", body, text=True)
        _write(output / "DELIVERY_BODY.md", delivery_body, text=True)
    manifest.update(selected_version=selected["run_id"], current_run_version=run_id,
        selected_input_matches_current=selected.get("input_hash") == input_hash and selected.get("guide_sha256") == guide_hash,
        selected_result_path=str(output / "runs" / selected["run_id"] / "FULL_BODY_RESULT.json"))
    checkpoint()
    public = [{k: v for k, v in row.items() if k != "result"} for row in manifest["stages"]]
    report = {**manifest, "stages": public}
    _write(run_dir / "IMPLEMENTATION_REPORT.json", report)
    _write(output / "IMPLEMENTATION_REPORT.json", report)
    return {**result, "stages": public, **{key: manifest[key] for key in
        ("selected_version", "selected_result_path", "selected_input_matches_current")}}
