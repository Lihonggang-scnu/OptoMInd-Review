"""Opt-in actual-body assessment and one bounded evidence-based repair.

The model report is a diagnostic, not scientific acceptance. Every request and
response is snapshot-bound and resumable; a partial or uncertain paid response
is never silently bought again. Original writer artifacts remain immutable.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from . import review_unit_writer as writer
from .article_text_editor import parse_edit_proposals, apply_text_edits
from .module4.runtime import invoke_client, recover_qwen_stream
from .writer_candidates_contracts import CandidateError, _handles

SCHEMA = "optomind.unit_realization.v1"
ASSESSMENT_PROMPT = """你是独立正文核查者。输入包含原始任务、完整对应材料和实际生成的正文。
只检查 actual_body_markdown 实际完成了什么；任务清单、引用出现和作者自称完成均不证明覆盖。
允许自然合并任务、段落和表格，不要求一个任务一个标题或段落。不要新检索，不使用外部答案。
逐一返回全部 task_catalog 中的 task_id，status 为 covered、partial、missing、material_limited。
covered 表示正文充分展开该任务；partial 表示已有实质内容但仍有明确遗漏；missing 表示未展开；
material_limited 表示所给材料不能支持需要补充的内容，不应强迫作者杜撰。
covered/partial 的 body_quote 必须是 actual_body_markdown 中的逐字片段；missing 可以为空。
explanation 必须具体说明覆盖或缺口，material_handles 只能指向本次实际材料中的正式 handle。
请勿因相邻单元预计会写而判定 covered；本次没有给出的生成正文不能证明 covered_elsewhere。
只在现有正文出现明确材料支持的实质错误时提出少量定点 replace；不作文风润色或整体重写。
每个 replace 使用 article_text_editor 格式 operation、original_text、replacement_text、reason，
附 material_handles 和逐字 material_quote 以便核对。original_text 必须在同一原始正文中唯一。
返回 JSON：{"tasks":[{"task_id":"...","status":"...","body_quote":"...",
"explanation":"...","material_handles":[]}],"changes":[],"issues":[]}。
该报告只是供人工复核的诊断，不能自行宣称科学质量通过。"""


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def assessment_messages(view, payload, body):
    tasks = []
    for field, id_field in (("paragraph_tasks", "paragraph_id"), ("table_tasks", "table_id")):
        for task in payload.get(field) or []:
            task_id = task.get(id_field)
            if not task_id:
                raise CandidateError("quality_explicit_task_identity_required")
            tasks.append({"task_id": task_id, "kind": field, "task": deepcopy(task)})
    return [{"role": "system", "content": ASSESSMENT_PROMPT},
            {"role": "user", "content": json.dumps({
                "schema_version": SCHEMA, "original_writer_payload": deepcopy(payload),
                "task_catalog": tasks, "actual_body_markdown": body,
                "known_material_handles": writer._known_unit_handles(view),
            }, ensure_ascii=False, indent=2)}]


def _find_quote_span(body, quote, cursor=0):
    start = body.find(quote, cursor)
    if start >= 0:
        return {"start": start, "end": start + len(quote), "text": quote}, False
    positions = [index for index in range(cursor, len(body)) if not body[index].isspace()]
    compact = "".join(body[index] for index in positions)
    target = "".join(char for char in quote if not char.isspace())
    start = compact.find(target) if target else -1
    if start < 0:
        return None, False
    left, right = positions[start], positions[start + len(target) - 1] + 1
    return {"start": left, "end": right, "text": body[left:right]}, True


def _body_quote_spans(body, quote):
    """Match original characters; only whitespace and explicit omissions vary."""
    if not quote:
        return [], "empty"
    fragments = [part.strip() for part in re.split(r"\.{3,}|…+", quote) if part.strip()]
    if not fragments:
        return [], "unmatched"
    span, normalized = _find_quote_span(body, quote)
    if span:
        return [span], "whitespace_normalized" if normalized else "exact"
    if not re.search(r"\.{3,}|…+", quote):
        return [], "unmatched"
    spans, cursor, normalized = [], 0, False
    for fragment in fragments:
        span, changed = _find_quote_span(body, fragment, cursor)
        if span is None:
            return [], "unmatched"
        cursor = span["end"]
        normalized = normalized or changed
        spans.append(span)
    return spans, "ordered_omission_excerpt_whitespace_normalized" if normalized else "ordered_omission_excerpt"


def _assessment_content(raw, complete):
    data = writer._decode_json_content(raw)
    if data is not None:
        return data, "writer_json_decoder"
    text = writer._strip_fences(raw, json_envelope=True).strip()
    if complete is True and text.startswith("{") and text.endswith("}"):
        # Do not let syntax recovery supply missing structure or content. The
        # existing helper inserts escapes only; its parsed content must agree
        # with the existing library's independent candidate.
        from .chapter_arrangement import _escape_inner_json_quotes
        from json_repair import repair_json
        try:
            mechanical = json.loads(_escape_inner_json_quotes(text), strict=False)
            candidate = repair_json(text, return_objects=True, strict=True)
            if candidate == mechanical:
                return candidate, "json_repair_quote_escape_crosschecked"
        except (ValueError, RecursionError):
            pass
    return None, "unparsed"


def validate_assessment(response, messages, view, body):
    if not isinstance(response, Mapping):
        raise CandidateError("quality_response_not_object")
    if response.get("complete") is False or response.get("finish_reason") != "stop":
        raise CandidateError("quality_response_incomplete")
    returned_model = response.get("returned_model") or response.get("model")
    if returned_model and returned_model != "qwen3.5-plus":
        raise CandidateError("quality_returned_model_mismatch")
    raw = response.get("content")
    if not isinstance(raw, str):
        raise CandidateError("quality_content_not_text")
    data, parsed_via = _assessment_content(raw, response.get("complete"))
    if not isinstance(data, Mapping) or not isinstance(data.get("tasks"), list):
        raise CandidateError("quality_assessment_not_json_tasks")
    expected = {row["task_id"] for row in json.loads(messages[-1]["content"])["task_catalog"]}
    known = set(writer._known_unit_handles(view))
    seen = set()
    rows = []
    for raw_row in data["tasks"]:
        if not isinstance(raw_row, Mapping):
            raise CandidateError("quality_task_not_object")
        row = deepcopy(dict(raw_row))
        task_id = row.get("task_id")
        if task_id not in expected or task_id in seen:
            raise CandidateError("quality_task_identity_invalid:" + str(task_id))
        seen.add(task_id)
        if row.get("status") not in {"covered", "partial", "missing", "material_limited"}:
            raise CandidateError("quality_task_status_invalid:" + str(task_id))
        quote = row.get("body_quote") or ""
        if not isinstance(quote, str):
            raise CandidateError("quality_body_quote_invalid:" + str(task_id))
        spans, match = _body_quote_spans(body, quote)
        if ((quote and not spans) or (row["status"] in {"covered", "partial"} and not quote.strip())):
            raise CandidateError("quality_body_quote_invalid:" + str(task_id))
        row["body_quote_spans"] = spans
        row["body_quote_match"] = match
        if not isinstance(row.get("explanation"), str) or not row["explanation"].strip():
            raise CandidateError("quality_task_explanation_missing:" + str(task_id))
        handles = row.get("material_handles")
        if not isinstance(handles, list) or any(handle not in known for handle in handles):
            raise CandidateError("quality_material_handle_invalid:" + str(task_id))
        rows.append(row)
    if seen != expected:
        raise CandidateError("quality_task_report_incomplete")
    return {"tasks": rows, "changes": deepcopy(data.get("changes") or []),
            "issues": deepcopy(data.get("issues") or []), "parsed_via": parsed_via,
            "scientific_acceptance": False}


def _material_quote_spans(record, quote):
    """Locate all excerpts inside one selected record, retaining field paths."""
    strings = []
    def collect(node, path):
        if isinstance(node, str):
            strings.append((path, node))
        elif isinstance(node, Mapping):
            for key, value in node.items():
                collect(value, [*path, key])
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                collect(value, [*path, index])
    collect(record, [])
    for path, text in strings:
        spans, match = _body_quote_spans(text, quote)
        if spans:
            return [{"path": path, **span} for span in spans], match
    fragments = [part.strip() for part in re.split(r"\.{3,}|…+", quote) if part.strip()]
    if not fragments:
        return [], "unmatched"
    spans, record_cursor, text_cursor, normalized = [], 0, 0, False
    for fragment in fragments:
        found = False
        for index in range(record_cursor, len(strings)):
            path, text = strings[index]
            span, changed = _find_quote_span(text, fragment, text_cursor if index == record_cursor else 0)
            if span:
                spans.append({"path": path, **span})
                record_cursor, text_cursor = index, span["end"]
                normalized = normalized or changed
                found = True
                break
        if not found:
            return [], "unmatched"
    return spans, "ordered_omission_excerpt_whitespace_normalized" if normalized else "ordered_omission_excerpt"


def apply_evidence_edits(body, changes, view):
    """Validate every anchor against one snapshot before using the editor."""
    parsed = parse_edit_proposals({"changes": changes})
    ranges = []
    known = set(writer._known_unit_handles(view))
    evidence_by_anchor = {}
    for change in parsed["changes"]:
        anchor = change["original_text"]
        handles = change.get("material_handles")
        quote = change.get("material_quote")
        if (change["operation"] != "replace" or body.count(anchor) != 1
                or not isinstance(handles, list) or not handles
                or any(handle not in known for handle in handles)
                or not str(change.get("reason") or "").strip()):
            raise CandidateError("quality_edit_anchor_or_evidence_invalid")
        match = None
        if isinstance(quote, str) and quote.strip():
            for container in ("materials", "chapter_tool_materials"):
                for index, record in enumerate(getattr(view, container)):
                    if not set(_handles(record)).intersection(handles):
                        continue
                    spans, kind = _material_quote_spans(record, quote)
                    if spans:
                        match = {"container": container, "record_index": index,
                                 "material_handles": handles, "match": kind, "spans": spans}
                        break
                if match:
                    break
        if match is None:
            raise CandidateError("quality_edit_material_quote_invalid")
        evidence_by_anchor[anchor] = match
        start = body.index(anchor)
        end = start + len(anchor)
        if any(start < prior_end and prior_start < end for prior_start, prior_end in ranges):
            raise CandidateError("quality_edit_anchors_overlap")
        ranges.append((start, end))
    # Applying from the end also prevents a replacement from becoming another
    # change's target. The existing editor applies the validated exact contract.
    ordered = sorted(parsed["changes"], key=lambda change: body.index(change["original_text"]), reverse=True)
    text, applied, skipped = apply_text_edits(body, ordered)
    if skipped:
        raise CandidateError("quality_edit_skipped_after_snapshot_validation")
    for item, change in zip(applied, ordered):
        item["material_quote_evidence"] = evidence_by_anchor[change["original_text"]]
    return text, applied


def _consume_post_edits(target, body, changes, view):
    """Consume a paid post-check once locally; no further assessment loop."""
    before, accepted, applied, rejected = body, [], [], []
    for index, change in enumerate(changes if isinstance(changes, list) else [changes]):
        try:
            apply_evidence_edits(before, [change], view)
            start = before.index(change["original_text"])
            accepted.append({"index": index, "change": change, "start": start,
                             "end": start + len(change["original_text"])})
        except Exception as exc:
            rejected.append({"proposal_index": index, "change": deepcopy(change),
                             "error": type(exc).__name__ + ":" + str(exc)})
    conflicts = {}
    for left_index, left in enumerate(accepted):
        for right in accepted[left_index + 1:]:
            if left["start"] < right["end"] and right["start"] < left["end"]:
                conflicts.setdefault(left["index"], []).append(right["index"])
                conflicts.setdefault(right["index"], []).append(left["index"])
    for item in accepted:
        if item["index"] in conflicts:
            rejected.append({"proposal_index": item["index"], "change": deepcopy(item["change"]),
                "error": "quality_edit_anchors_overlap", "conflicting_proposal_indices": conflicts[item["index"]]})
    accepted = [item for item in accepted if item["index"] not in conflicts]
    if accepted:
        try:
            body, applied = apply_evidence_edits(before, [item["change"] for item in accepted], view)
            for result, item in zip(applied, sorted(accepted, key=lambda row: row["start"], reverse=True)):
                result["proposal_index"] = item["index"]
        except Exception as exc:
            body, applied = before, []
            rejected.extend({"proposal_index": item["index"], "change": deepcopy(item["change"]),
                "error": type(exc).__name__ + ":" + str(exc)} for item in accepted)
    status = ("partially_applied_pending_human_review" if applied and rejected else
              "applied_pending_human_review" if applied else "rejected")
    audit = {"status": status, "changes": deepcopy(changes), "applied": applied,
        "rejected": sorted(rejected, key=lambda row: row["proposal_index"]),
        "error": str(len(rejected)) + " post proposal(s) rejected" if rejected else "",
        "assessment_task_status_basis": "before_post_edits",
        "before_body_sha256": hashlib.sha256(before.encode("utf-8")).hexdigest(),
        "after_body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "scientific_acceptance": False, "model_calls": 0}
    directory = target / "post_edits" / _hash(audit)
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in (("BEFORE.md", before), ("AFTER.md", body)):
        path, content = directory / name, text.encode("utf-8")
        if path.exists() and path.read_bytes() != content:
            raise CandidateError("quality_post_edit_snapshot_integrity_failure")
        if not path.exists():
            path.write_bytes(content)
    path = directory / "POST_EDIT_LOG.json"
    if path.exists() and _read(path) != audit:
        raise CandidateError("quality_post_edit_log_integrity_failure")
    if not path.exists():
        _write(path, audit)
    return body, {**audit, "output_dir": str(directory), "log_path": str(path),
        "before_path": str(directory / "BEFORE.md"), "after_path": str(directory / "AFTER.md")}


def _cached_call(step, messages, profile, client_factory, run, token_counter, execute=None):
    from .legacy_unit_route import _CaptureClient
    step.mkdir(parents=True, exist_ok=True)
    request = {"profile": profile, "messages_sha256": _hash(messages)}
    request_path = step / "REQUEST.json"
    existed = request_path.exists()
    if existed and _read(request_path) != request:
        raise CandidateError("quality_cached_request_identity_changed")
    _write(step / "MESSAGES.json", messages)
    _write(step / "PROFILE.json", profile)
    estimate = writer.estimate_unit_cost(messages, model=profile["model"],
        output_tokens=profile["max_output_tokens"], thinking_budget=profile["thinking_budget"],
        token_counter=token_counter)
    _write(step / "ESTIMATE.json", estimate)
    raw_path = step / "RAW_RESPONSE.json"
    if not raw_path.exists() and existed:
        streams = sorted((step / "transport").glob("*.sse.*"))
        if len(streams) == 1:
            _write(raw_path, recover_qwen_stream(streams[0]))
    raw = _read(raw_path) if raw_path.exists() else None
    if raw is None and (existed or not run or estimate["input_capacity"]["exceeds_capacity"]):
        return None, 0, "pending_existing_attempt" if existed else "capacity_blocked" if estimate["input_capacity"]["exceeds_capacity"] else "planned"
    calls = 0
    if raw is None:
        _write(request_path, request)  # Persist before factory/key/provider access.
        client = client_factory(step.name, step, deepcopy(profile))
        if getattr(client, "max_retries", 0) != 0:
            raise CandidateError("quality_automatic_paid_retries_forbidden")
        capture = _CaptureClient(client, step, _hash(request)[:16] + "-" + step.name)
        try:
            if execute is None:
                raw = invoke_client(capture, messages, model=profile["model"],
                    max_output_tokens=profile["max_output_tokens"], thinking=profile["thinking"],
                    thinking_budget=profile["thinking_budget"])
                value = raw
            else:
                value = execute(capture)
                raw = _read(raw_path) if raw_path.exists() else None
        except Exception as exc:
            _write(step / "STAGE_ERROR.json", {"error": type(exc).__name__ + ":" + str(exc)})
            return None, capture.invocations, "failed_saved_attempt"
        calls = capture.invocations
    else:
        if execute is None:
            value = raw
        else:
            def replay(messages, **kwargs):
                return deepcopy(raw)
            replay.prompt_token_counter = token_counter
            value = execute(replay)
    return value, calls, "returned" if calls else "cache_hit"


def run_unit_quality(view, *, payload, existing_body, output_dir, client_factory=None,
                     run=False, token_counter=None, language="zh", writer_profile=None,
                     experiment_label="production_actual_body", reparse_saved=False):
    """Callable production quality stage, also usable for labeled repair studies."""
    from .legacy_unit_route import _profile
    author = writer_profile or _profile("qwen3.5-plus", 32768, 8192)
    reviewer = _profile("qwen3.5-plus", 24576, 16384)
    identity = {"schema_version": SCHEMA, "payload_sha256": _hash(payload),
                "original_body_sha256": _hash(existing_body), "author_profile": author,
                "reviewer_profile": reviewer, "prompt_sha256": _hash(ASSESSMENT_PROMPT),
                "completion_prompt_sha256": _hash(writer.load_writer_prompt(planning_revision=True)
                                                  + writer._COMPLETION_INSTRUCTIONS),
                "experiment_label": experiment_label}
    target = Path(output_dir).resolve() / _hash(identity)
    result_path = target / "QUALITY_RESULT.json"
    seal_path = target / "RESULT_SEAL.json"
    prior_snapshot = None
    if result_path.exists() and seal_path.exists():
        saved = _read(result_path)
        body_path = target / "QUALITY_BODY.md"
        if (_read(seal_path).get("result_sha256") != _hash(saved) or not body_path.exists()
                or _read(seal_path).get("body_sha256") != hashlib.sha256(body_path.read_bytes()).hexdigest()):
            raise CandidateError("quality_cached_result_integrity_failure")
        # A preview may be followed by a run, but a terminal paid-stage failure
        # is retained. Raw recovery only re-parses, never dispatches again.
        if not reparse_saved and (saved["status"] != "planned" or not run):
            return {**saved, "model_calls": 0, "cache_hit": True}
        if reparse_saved:
            history = target / "reparse_history" / _hash(saved)
            # Keep the exact prior summary, seal, body and parsed diagnostics.
            # Saved RAW and request identities remain in their original steps.
            for relative in ("QUALITY_RESULT.json", "RESULT_SEAL.json", "QUALITY_BODY.md",
                             "assessment/ASSESSMENT.json", "post_assessment/ASSESSMENT.json"):
                source = target / relative
                if source.is_file():
                    destination = history / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    original = source.read_bytes()
                    if destination.exists() and destination.read_bytes() != original:
                        raise CandidateError("quality_reparse_history_integrity_failure")
                    if not destination.exists():
                        destination.write_bytes(original)
            prior_snapshot = {"prior_result_sha256": _hash(saved), "history_dir": str(history)}
    target.mkdir(parents=True, exist_ok=True)
    _write(target / "IDENTITY.json", identity)
    _write(target / "ORIGINAL_PAYLOAD.json", payload)
    (target / "ORIGINAL_BODY.md").write_bytes(existing_body.encode("utf-8"))
    summary = {"schema_version": SCHEMA, "identity": identity, "output_dir": str(target),
               "body_markdown": existing_body, "model_calls": 0, "cache_hit": False,
               "status": "planned", "pending_problems": [], "assessments": [],
               "changed": False, "scientific_acceptance": False, "applied_edits": []}
    if prior_snapshot:
        summary["reparse_saved"] = prior_snapshot
    waiting_to_run = False

    def assess(body, name):
        nonlocal waiting_to_run
        messages = assessment_messages(view, payload, body)
        raw, calls, state = _cached_call(target / name, messages, reviewer, client_factory, run, token_counter)
        summary["model_calls"] += calls
        if raw is None:
            waiting_to_run = waiting_to_run or state == "planned"
            summary["status"] = "planned" if state == "planned" else "pending"
            summary["pending_problems"].append({"code": "quality_assessment_" + state, "step": name})
            return None
        assessment = validate_assessment(raw, messages, view, body)
        _write(target / name / "ASSESSMENT.json", assessment)
        summary["assessments"].append({"step": name, "report": assessment})
        return assessment

    try:
        assessment = assess(existing_body, "assessment")
        if assessment is not None:
            body = existing_body
            candidate_base = None
            if assessment["changes"]:
                try:
                    body, summary["applied_edits"] = apply_evidence_edits(body, assessment["changes"], view)
                except Exception as exc:
                    summary["pending_problems"].append({"code": "quality_edit_rejected", "error": str(exc)})
            requested = [row["task_id"] for row in assessment["tasks"] if row["status"] in {"partial", "missing"}]
            if requested:
                # Parser-added span diagnostics must not change a saved
                # completion request's original model-feedback identity.
                feedback = [{key: deepcopy(value) for key, value in row.items()
                             if key not in {"body_quote_spans", "body_quote_match"}}
                            for row in assessment["tasks"] if row["task_id"] in requested]
                messages = writer.completion_messages(view, body, requested, language=language,
                                                      planning_revision=True, gap_feedback=feedback)
                def complete(client):
                    return writer.run_unit_completion(view, existing_body=body, task_ids=requested,
                        client=client, model=author["model"], language=language, planning_revision=True,
                        output_tokens=author["max_output_tokens"], thinking_budget=author["thinking_budget"],
                        thinking=author["thinking"], raw_response_dir=target / "completion" / "raw",
                        gap_feedback=feedback)
                result, calls, state = _cached_call(target / "completion", messages, author, client_factory,
                                                   run, token_counter, execute=complete)
                summary["model_calls"] += calls
                if result is not None:
                    estimate = _read(target / "completion" / "ESTIMATE.json")
                    report = writer.write_unit_completion(view, result, target / "completion", estimate=estimate, language=language)
                    summary["completion"] = report
                    if not result["pending"]:
                        body = result["body_markdown"]
                    elif result.get("candidate_eligible"):
                        candidate_base, body = body, result["candidate_body_markdown"]
                        summary["completion_candidate"] = {"status": "awaiting_post_assessment",
                            "body_path": report["candidate_body_path"], "parsed_via": report["parsed_via"],
                            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                            "model_status_present": report["model_status_present"],
                            "scientific_acceptance": False}
                        summary["pending_problems"].append({"code": "quality_completion_candidate_metadata_missing",
                            "issues": result["issues"], "note": "Candidate requires the existing post-assessment; no coverage inferred."})
                    else:
                        summary["pending_problems"].append({"code": "quality_completion_pending", "issues": result["issues"]})
                else:
                    waiting_to_run = waiting_to_run or state == "planned"
                    summary["pending_problems"].append({"code": "quality_completion_" + state})
            summary["body_markdown"] = body
            summary["changed"] = body != existing_body
            if summary["changed"]:
                try:
                    assessment = assess(body, "post_assessment")
                except Exception:
                    if candidate_base is not None:
                        summary["body_markdown"] = candidate_base
                        summary["changed"] = candidate_base != existing_body
                        summary["completion_candidate"]["status"] = "post_assessment_failed"
                    raise
                if candidate_base is not None:
                    if assessment is None:
                        body = candidate_base
                        summary["body_markdown"] = body
                        summary["changed"] = body != existing_body
                    else:
                        summary["completion_candidate"]["status"] = "adopted_pending_human_review"
                if assessment is not None and assessment["changes"]:
                    body, post_edits = _consume_post_edits(target, body, assessment["changes"], view)
                    summary["post_edits"] = post_edits
                    summary["body_markdown"] = body
                    summary["changed"] = body != existing_body
                    details = {"log_path": post_edits["log_path"],
                        "assessment_task_status_basis": "before_post_edits",
                        "note": "Pre-edit task/issue diagnostics retained; no further model call or scientific acceptance."}
                    if post_edits["applied"]:
                        summary["pending_problems"].append({**details,
                            "code": "quality_postcheck_edits_applied_pending_review", "count": len(post_edits["applied"])})
                    if post_edits["rejected"]:
                        summary["pending_problems"].append({**details,
                            "code": "quality_postcheck_edits_not_applied", "rejected": post_edits["rejected"]})
            if assessment is not None:
                summary["pending_problems"].extend({"code": "quality_task_" + row["status"], **row}
                    for row in assessment["tasks"] if row["status"] != "covered")
                summary["pending_problems"].extend({"code": "quality_assessor_issue", "detail": issue}
                    for issue in assessment["issues"])
                summary["status"] = "assessed_with_pending" if summary["pending_problems"] else "assessed_pending_human_review"
    except Exception as exc:
        summary["status"] = "pending"
        summary["pending_problems"].append({"code": "quality_stage_failed", "error": type(exc).__name__ + ":" + str(exc)})
    if waiting_to_run and not run and summary["status"] != "pending":
        summary["status"] = "planned"
    (target / "QUALITY_BODY.md").write_bytes(summary["body_markdown"].encode("utf-8"))
    _write(result_path, summary)
    _write(seal_path, {"result_sha256": _hash(summary),
        "body_sha256": hashlib.sha256((target / "QUALITY_BODY.md").read_bytes()).hexdigest()})
    return summary


def retained_original_issues(view, original_issues, quality_result):
    """Resolve only old missing-table syntax flags verified on the current body.

    Task coverage remains the independently reported task status. Multiple
    table tasks may be realized by one valid table; no table-count rule exists.
    Scientific and other unresolved original issues are retained verbatim.
    """
    retained, resolved = [], []
    reports = quality_result.get("assessments") or []
    final = reports[-1].get("report", {}) if reports else {}
    statuses = {row.get("task_id"): row.get("status") for row in final.get("tasks") or []}
    table_ids = [task.get("table_id") for task in view.table_tasks]
    table_check = writer._table_consumption_report(quality_result["body_markdown"], bool(view.table_tasks))
    current_assessment = (not quality_result.get("changed") or
                          bool(reports and reports[-1].get("step") == "post_assessment"))
    verified = (current_assessment and bool(table_ids) and
                all(task_id and statuses.get(task_id) == "covered" for task_id in table_ids)
                and table_check["table_check"].get("valid") is True)
    for issue in original_issues:
        if (verified and isinstance(issue, Mapping) and issue.get("code") in
                {"markdown_table_missing_or_invalid", "table_markdown_missing_or_invalid"}):
            resolved.append(deepcopy(issue))
        else:
            retained.append(deepcopy(issue))
    return retained, resolved
