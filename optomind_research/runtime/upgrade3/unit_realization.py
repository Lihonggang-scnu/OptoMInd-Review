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
    data = writer._decode_json_content(raw)
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
        if (not isinstance(quote, str) or (quote and quote not in body)
                or (row["status"] in {"covered", "partial"} and not quote.strip())):
            raise CandidateError("quality_body_quote_invalid:" + str(task_id))
        if not isinstance(row.get("explanation"), str) or not row["explanation"].strip():
            raise CandidateError("quality_task_explanation_missing:" + str(task_id))
        handles = row.get("material_handles")
        if not isinstance(handles, list) or any(handle not in known for handle in handles):
            raise CandidateError("quality_material_handle_invalid:" + str(task_id))
        rows.append(row)
    if seen != expected:
        raise CandidateError("quality_task_report_incomplete")
    return {"tasks": rows, "changes": deepcopy(data.get("changes") or []),
            "issues": deepcopy(data.get("issues") or []), "scientific_acceptance": False}


def apply_evidence_edits(body, changes, view):
    """Validate every anchor against one snapshot before using the editor."""
    parsed = parse_edit_proposals({"changes": changes})
    ranges = []
    known = set(writer._known_unit_handles(view))

    def contains_quote(node, quote):
        if isinstance(node, str):
            return quote in node
        if isinstance(node, Mapping):
            return any(contains_quote(value, quote) for value in node.values())
        if isinstance(node, (list, tuple)):
            return any(contains_quote(value, quote) for value in node)
        return False
    for change in parsed["changes"]:
        anchor = change["original_text"]
        handles = change.get("material_handles")
        quote = change.get("material_quote")
        if (change["operation"] != "replace" or body.count(anchor) != 1
                or not isinstance(handles, list) or not handles
                or any(handle not in known for handle in handles)
                or not str(change.get("reason") or "").strip()):
            raise CandidateError("quality_edit_anchor_or_evidence_invalid")
        evidence = [item for item in view.materials if set(_handles(item)).intersection(handles)]
        evidence.extend(item for item in view.chapter_tool_materials if set(_handles(item)).intersection(handles))
        if not isinstance(quote, str) or not quote.strip() or not any(
                contains_quote(item, quote) for item in evidence):
            raise CandidateError("quality_edit_material_quote_invalid")
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
    return text, applied


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
                     experiment_label="production_actual_body"):
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
    if result_path.exists() and seal_path.exists():
        saved = _read(result_path)
        body_path = target / "QUALITY_BODY.md"
        if (_read(seal_path).get("result_sha256") != _hash(saved) or not body_path.exists()
                or _read(seal_path).get("body_sha256") != hashlib.sha256(body_path.read_bytes()).hexdigest()):
            raise CandidateError("quality_cached_result_integrity_failure")
        # A preview may be followed by a run, but a terminal paid-stage failure
        # is retained. Raw recovery only re-parses, never dispatches again.
        if saved["status"] != "planned" or not run:
            return {**saved, "model_calls": 0, "cache_hit": True}
    target.mkdir(parents=True, exist_ok=True)
    _write(target / "IDENTITY.json", identity)
    _write(target / "ORIGINAL_PAYLOAD.json", payload)
    (target / "ORIGINAL_BODY.md").write_bytes(existing_body.encode("utf-8"))
    summary = {"schema_version": SCHEMA, "identity": identity, "output_dir": str(target),
               "body_markdown": existing_body, "model_calls": 0, "cache_hit": False,
               "status": "planned", "pending_problems": [], "assessments": [],
               "changed": False, "scientific_acceptance": False, "applied_edits": []}

    def assess(body, name):
        messages = assessment_messages(view, payload, body)
        raw, calls, state = _cached_call(target / name, messages, reviewer, client_factory, run, token_counter)
        summary["model_calls"] += calls
        if raw is None:
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
            if assessment["changes"]:
                try:
                    body, summary["applied_edits"] = apply_evidence_edits(body, assessment["changes"], view)
                except Exception as exc:
                    summary["pending_problems"].append({"code": "quality_edit_rejected", "error": str(exc)})
            requested = [row["task_id"] for row in assessment["tasks"] if row["status"] in {"partial", "missing"}]
            if requested:
                feedback = [deepcopy(row) for row in assessment["tasks"] if row["task_id"] in requested]
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
                    else:
                        summary["pending_problems"].append({"code": "quality_completion_pending", "issues": result["issues"]})
                else:
                    summary["pending_problems"].append({"code": "quality_completion_" + state})
            summary["body_markdown"] = body
            summary["changed"] = body != existing_body
            if summary["changed"]:
                assessment = assess(body, "post_assessment")
            if assessment is not None:
                summary["pending_problems"].extend({"code": "quality_task_" + row["status"], **row}
                    for row in assessment["tasks"] if row["status"] != "covered")
                summary["pending_problems"].extend({"code": "quality_assessor_issue", "detail": issue}
                    for issue in assessment["issues"])
                summary["status"] = "assessed_with_pending" if summary["pending_problems"] else "assessed_pending_human_review"
    except Exception as exc:
        summary["status"] = "pending"
        summary["pending_problems"].append({"code": "quality_stage_failed", "error": type(exc).__name__ + ":" + str(exc)})
    (target / "QUALITY_BODY.md").write_bytes(summary["body_markdown"].encode("utf-8"))
    _write(result_path, summary)
    _write(seal_path, {"result_sha256": _hash(summary),
        "body_sha256": hashlib.sha256((target / "QUALITY_BODY.md").read_bytes()).hexdigest()})
    return summary
