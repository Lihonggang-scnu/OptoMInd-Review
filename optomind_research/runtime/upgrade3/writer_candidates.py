"""Optional, auditable chapter writing routes; the existing planner is untouched.

All partitioning is over complete tasks or complete prose blocks. Preview never
constructs a client. Paid dispatch occurs once per uncached stage; retrying an
uncertain, incomplete or failed attempt requires an explicit retry_failed flag.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .module4.runtime import (build_qwen_wire_body, estimate_prompt_tokens,
                              estimated_cost_cny, invoke_client, model_pricing)
from .writer_candidates_contracts import (CandidateError, parse_candidate_response,
                                          project_chapter, render_blocks, task_catalog)

SCHEMA_VERSION = "optomind.writer_candidates.v1"
ROUTES = ("chapter", "units_edit", "hierarchical")
ROOT = Path(__file__).resolve().parents[3]
PROMPT_ROOT = ROOT / "prompts" / "writer_candidates"
DEFAULT_PROFILE = {"model": "qwen3.5-plus", "thinking": True,
                   "thinking_budget": 16384, "max_output_tokens": 32768,
                   "json_mode": False, "timeout_seconds": 300.0,
                   "stream": True, "stream_overall_timeout_seconds": 3600.0,
                   "prompt_token_multiplier": 1.12, "prompt_token_framing_margin": 8192}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _write(path: Path, value: Any, *, text: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(str(value) if text else json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@contextmanager
def _output_lock(output: Path):
    """OS-released lock prevents two processes charging for one stage."""
    output.mkdir(parents=True, exist_ok=True)
    handle = (output / ".writer_candidates.lock").open("a+b")
    try:
        if os.name == "nt":  # pragma: no cover - exercised on Windows deployments
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise CandidateError("candidate_output_in_use") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CandidateError("candidate_output_in_use") from exc
        yield
    finally:
        handle.close()


def _profile(raw: Mapping[str, Any], role: str) -> dict[str, Any]:
    profile = {**DEFAULT_PROFILE, **dict(raw)}
    allowed = set(DEFAULT_PROFILE)
    if set(profile) - allowed:
        raise CandidateError(f"unknown_profile_keys:{role}:{sorted(set(profile) - allowed)}")
    try:
        for name in ("thinking", "json_mode", "stream"):
            if not isinstance(profile[name], bool):
                raise ValueError(name)
        for name in ("thinking_budget", "max_output_tokens", "prompt_token_framing_margin"):
            if isinstance(profile[name], bool) or int(profile[name]) != profile[name]:
                raise ValueError(name)
            profile[name] = int(profile[name])
        for name in ("timeout_seconds", "stream_overall_timeout_seconds", "prompt_token_multiplier"):
            profile[name] = float(profile[name])
            if not math.isfinite(profile[name]):
                raise ValueError(name)
        if profile["thinking_budget"] < 0 or profile["max_output_tokens"] < 64:
            raise ValueError("token_limits")
        if not profile["thinking"]:
            profile["thinking_budget"] = 0
        if profile["thinking"] and not profile["thinking_budget"]:
            raise ValueError("thinking_requires_budget")
        if profile["timeout_seconds"] < 5 or profile["stream_overall_timeout_seconds"] < profile["timeout_seconds"]:
            raise ValueError("timeout")
        if profile["prompt_token_multiplier"] < 1 or profile["prompt_token_framing_margin"] < 0:
            raise ValueError("estimator")
        if not profile["stream"]:
            raise ValueError("stream_must_be_true")
        caps = model_pricing(profile["model"])
        if profile["max_output_tokens"] + profile["thinking_budget"] > caps["max_output_tokens"]:
            raise ValueError("model_total_output_exceeded")
        if profile["thinking"] and profile["json_mode"] and not caps["thinking_json"]:
            raise ValueError("thinking_json_unsupported_for_model")
    except Exception as exc:
        raise CandidateError(f"invalid_profile:{role}:{exc}") from exc
    return profile


def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate explicit roles and provider capacities without a client or key."""
    if not isinstance(config, Mapping):
        raise CandidateError("config_must_be_object")
    unknown = set(config) - {"schema_version", "purpose", "writer", "editor", "max_input_tokens", "editor_pass"}
    if unknown:
        raise CandidateError("unknown_config_keys:" + ",".join(sorted(unknown)))
    normalized = deepcopy(dict(config))
    for role in ("writer", "editor"):
        raw = config.get(role, {})
        if not isinstance(raw, Mapping):
            raise CandidateError("profile_must_be_object:" + role)
        normalized[role] = _profile(raw, role)
    max_input = config.get("max_input_tokens")
    if max_input is not None:
        if isinstance(max_input, bool) or not isinstance(max_input, (int, float)) or int(max_input) != max_input or max_input <= 0:
            raise CandidateError("max_input_tokens_must_be_positive_integer")
        normalized["max_input_tokens"] = int(max_input)
    if not isinstance(config.get("editor_pass", True), bool):
        raise CandidateError("editor_pass_must_be_boolean")
    normalized["editor_pass"] = config.get("editor_pass", True)
    return normalized


def _source_file_hashes() -> dict[str, str]:
    paths = (Path(__file__), Path(__file__).with_name("writer_candidates_contracts.py"),
             Path(__file__).with_name("review_unit_writer.py"), Path(__file__).parent / "module4/runtime.py")
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def _code_hash() -> str:
    # Used prompt bytes already live in the exact messages. Keeping unrelated
    # prompts out of this common hash allows editor-only changes to reuse drafts.
    return _hash(_source_file_hashes())


def _git_commit() -> str | None:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                                capture_output=True, timeout=3, check=True)
        value = result.stdout.strip()
        return value if len(value) == 40 and all(c in "0123456789abcdef" for c in value) else None
    except (OSError, subprocess.SubprocessError):
        return None


def _messages(chapter: Mapping[str, Any], ids: Sequence[str], route: str, *,
              drafts: Sequence[Mapping[str, Any]] | None = None,
              neighbors: Sequence[Mapping[str, Any]] = (), edit_scope: str = "chapter") -> list[dict[str, str]]:
    payload = project_chapter(chapter, list(ids))
    payload["language"] = chapter.get("language", "zh")
    if drafts is None:
        prompt = (PROMPT_ROOT / "writer.md").read_text(encoding="utf-8")
        prompt += "\n\n" + (PROMPT_ROOT / f"{route}.md").read_text(encoding="utf-8")
    else:
        prompt = (PROMPT_ROOT / "editor.md").read_text(encoding="utf-8")
        payload.update(draft_blocks=deepcopy(list(drafts)), read_only_neighbors=deepcopy(list(neighbors)),
                       edit_scope=edit_scope,
                       pending_task_ids=[key for block in drafts for key in block.get("pending_task_ids", [])])
    return [{"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)}]


def _estimate(messages: Sequence[Mapping[str, Any]], profile: Mapping[str, Any],
              max_input: int | None, counter: Any = None) -> dict[str, Any]:
    raw, wire = build_qwen_wire_body(messages, **{k: profile[k] for k in
        ("model", "max_output_tokens", "thinking", "thinking_budget", "json_mode", "stream")})
    estimate = estimate_prompt_tokens(raw, messages, prompt_token_counter=counter,
                                     prompt_token_multiplier=profile["prompt_token_multiplier"],
                                     prompt_token_framing_margin=profile["prompt_token_framing_margin"])
    caps = model_pricing(profile["model"])
    reserve = int(estimate["prompt_tokens"])
    completion = profile["max_output_tokens"] + profile["thinking_budget"]
    limit = min(caps["max_input_tokens"], caps["context_window"] - completion)
    if max_input is not None:
        limit = min(limit, max_input)
    return {**estimate, "reserved_input_tokens": reserve, "input_limit_tokens": limit, "completion_allowance_tokens": completion,
            "context_window": caps["context_window"], "fits": reserve <= limit,
            "estimated_max_cost_cny": estimated_cost_cny(
                {"prompt_tokens": reserve, "completion_tokens": completion}, model=profile["model"], conservative=True),
            "wire_request_sha256": hashlib.sha256(raw).hexdigest(),
            "wire_parameters": {k: v for k, v in wire.items() if k != "messages"}}


def _material_manifest(chapter: Mapping[str, Any], ids: Sequence[str]) -> dict[str, Any]:
    projection = project_chapter(chapter, list(ids))
    sources = projection.get("sources", [])
    return {"editable_task_ids": list(ids), "task_hashes": {key: _hash(value) for key, value in task_catalog(chapter).items() if key in ids},
            "source_records": [{"source_handle": row.get("source_handle", row.get("handle", "")),
                                "sha256": _hash(row), "utf8_bytes": len(_json(row).encode("utf-8"))}
                               for row in sources if isinstance(row, Mapping)],
            "chapter_tool_materials_sha256": _hash(projection.get("chapter_tool_materials", [])),
            "material_preserved": True, "task_atom_splitting": False}


def _stage(stage_id: str, role: str, ids: Sequence[str], messages: list[dict[str, str]],
           profile: dict[str, Any], estimate: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"stage_id": stage_id, "role": role, "task_ids": list(ids), "messages": messages,
            "effective_profile": profile, "estimate": estimate,
            "status": "ready" if estimate["fits"] else "capacity_blocked", **extra}


def _writer_plan(chapter: dict[str, Any], route: str, profile: dict[str, Any],
                 max_input: int | None, counter: Any) -> list[dict[str, Any]]:
    catalog = task_catalog(chapter)
    all_ids = list(catalog)
    if not all_ids:
        raise CandidateError("chapter_has_no_tasks")
    groups: list[list[str]] = []
    for unit in chapter["units"]:
        ids = [key for key, value in catalog.items() if value["unit_id"] == unit["unit_id"]]
        if ids:
            groups.append(ids)

    def make(ids: list[str], index: int) -> dict[str, Any]:
        messages = _messages(chapter, ids, route)
        stage_id = "writer_chapter" if route == "chapter" else f"writer_{'unit' if route == 'units_edit' else 'cluster'}_{index:03d}"
        return _stage(stage_id, "writer", ids, messages, profile,
                      _estimate(messages, profile, max_input, counter), material_manifest=_material_manifest(chapter, ids))

    if route == "chapter":
        return [make(all_ids, 1)]
    if route == "units_edit":
        return [make(ids, index) for index, ids in enumerate(groups, 1)]
    # Prefer intact units, then split an oversized unit only at complete tasks.
    atoms: list[list[str]] = []
    for ids in groups:
        if make(ids, 1)["estimate"]["fits"]:
            atoms.append(ids)
        else:
            atoms.extend([[key] for key in ids])
    packed: list[dict[str, Any]] = []
    current: list[str] = []
    for atom in atoms:
        candidate = make(current + atom, len(packed) + 1)
        if candidate["estimate"]["fits"]:
            current.extend(atom)
        else:
            if current:
                packed.append(make(current, len(packed) + 1))
                current = []
            single = make(atom, len(packed) + 1)
            if single["estimate"]["fits"]:
                current = list(atom)
            else:
                single["required_action"] = "Provide a model/profile with sufficient intact-task context, or revise the approved task/material scope upstream; no evidence was truncated."
                packed.append(single)
    if current:
        packed.append(make(current, len(packed) + 1))
    return packed


def _scoped_ids(blocks: Sequence[Mapping[str, Any]], catalog: Mapping[str, Any]) -> list[str]:
    claimed = {key for block in blocks for key in block.get("task_ids", []) + block.get("draft_scope_task_ids", [])}
    return [key for key in catalog if key in claimed]


def _editor_plan(chapter: dict[str, Any], blocks: list[dict[str, Any]], route: str,
                 profile: dict[str, Any], max_input: int | None, counter: Any) -> list[dict[str, Any]]:
    catalog = task_catalog(chapter)

    def make(selected: list[dict[str, Any]], stage_id: str, *, neighbors=(), scope="window", start=0, end=0):
        ids = _scoped_ids(selected, catalog)
        messages = _messages(chapter, ids, route, drafts=selected, neighbors=neighbors, edit_scope=scope)
        return _stage(stage_id, "editor", ids, messages, profile,
                      _estimate(messages, profile, max_input, counter), block_start=start, block_end=end,
                      material_manifest=_material_manifest(chapter, ids), edit_scope=scope,
                      neighbor_block_ids=[b["block_id"] for b in neighbors])

    whole = make(blocks, "editor_chapter", scope="chapter", end=len(blocks))
    if whole["estimate"]["fits"]:
        return [whole]
    # Blocks sharing a task must be edited together. Preserve contiguous spans
    # across overlapping claims rather than arbitrarily splitting arguments.
    last_for_task = {key: i for i, block in enumerate(blocks) for key in block.get("task_ids", []) + block.get("draft_scope_task_ids", [])}
    spans: list[tuple[int, int]] = []
    start = 0
    while start < len(blocks):
        end = start + 1
        index = start
        while index < end:
            for key in blocks[index].get("task_ids", []) + blocks[index].get("draft_scope_task_ids", []):
                end = max(end, last_for_task[key] + 1)
            index += 1
        spans.append((start, end))
        start = end
    windows: list[tuple[int, int]] = []
    pending: tuple[int, int] | None = None
    for start, end in spans:
        proposed = (pending[0], end) if pending else (start, end)
        stage = make(blocks[proposed[0]:proposed[1]], "editor_window_001", start=proposed[0], end=proposed[1])
        if stage["estimate"]["fits"]:
            pending = proposed
        else:
            if pending:
                windows.append(pending)
            # A single span may still exceed capacity; keep its full request
            # and pause that scope, preserving all draft text and other edits.
            pending = (start, end)
    if pending:
        windows.append(pending)
    stages = []
    for index, (start, end) in enumerate(windows, 1):
        stage_id = f"editor_window_{index:03d}"
        stage = make(blocks[start:end], stage_id, start=start, end=end)
        neighbors: list[dict[str, Any]] = []
        omitted = []
        for neighbor_index in (start - 1, end):
            if not 0 <= neighbor_index < len(blocks):
                continue
            neighbor = blocks[neighbor_index]
            with_neighbor = make(blocks[start:end], stage_id, neighbors=neighbors + [neighbor], start=start, end=end)
            if with_neighbor["estimate"]["fits"]:
                neighbors.append(neighbor)
                stage = with_neighbor
            else:
                omitted.append(neighbor["block_id"])
        stage["omitted_read_only_neighbor_block_ids"] = omitted
        if not stage["estimate"]["fits"]:
            stage["capacity_reason"] = ("shared_task_references_bind_multiple_blocks" if end - start > 1 else "single_complete_prose_task_span")
            stage["required_action"] = "This complete prose/task span plus its full evidence exceeds editor context. Increase editor capacity or revise the approved scope; the original draft remains selected."
        stages.append(stage)
    return stages


def _safe_error(exc: Exception) -> dict[str, Any]:
    record = getattr(exc, "record", None)
    return {"error_type": type(exc).__name__, "error": str(exc),
            "record": deepcopy(dict(record)) if isinstance(record, Mapping) else None}


def _parse(response: Any, chapter: dict[str, Any], ids: list[str], call_error: Any = None) -> dict[str, Any]:
    try:
        result = parse_candidate_response(response, chapter, ids)
    except Exception as exc:
        result = {"blocks": [], "body_markdown": "", "issues": [{"code": "response_parse_failed", "error_type": type(exc).__name__, "detail": str(exc)}],
                  "pending_task_ids": ids, "complete": False, "diagnostics": {}, "raw_response": response}
    result["transport_complete"] = not call_error and not result.get("diagnostics", {}).get("transport_incomplete", False)
    if call_error:
        result["call_error"] = call_error
        result.setdefault("issues", []).append({"code": "stage_call_failed", **call_error})
        result["complete"] = False
    if isinstance(response, Mapping) and (response.get("complete") is False or response.get("finish_reason") in ("length", "content_filter", "error")):
        result["complete"] = False
    return result


def _check_client(client: Any, profile: Mapping[str, Any]) -> None:
    for key in ("model", "json_mode"):
        if hasattr(client, key) and getattr(client, key) != profile[key]:
            raise CandidateError(f"client_profile_mismatch:{key}")
    if hasattr(client, "max_retries") and client.max_retries != 0:
        raise CandidateError("automatic_paid_retries_are_disabled")


def _execute_stage(stage: dict[str, Any], *, chapter: dict[str, Any], output: Path,
                   run_dir: Path, route: str, code_hash: str, input_hash: str,
                   dependencies: list[dict[str, str]], client_factory: Callable[..., Any] | None,
                   run: bool, retry_failed: bool, max_input: int | None, counter: Any) -> dict[str, Any]:
    signature = {"schema_version": SCHEMA_VERSION, "stage_id": stage["stage_id"], "route": route,
                 "execution_mode": getattr(client_factory, "execution_mode", "injected" if client_factory else "preview"),
                 "recording_fixture_sha256": getattr(client_factory, "fixture_sha256", None),
                 "messages": stage["messages"], "effective_profile": stage["effective_profile"],
                 "code_hash": code_hash, "input_hash": input_hash, "dependencies": dependencies}
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
    if attempts:
        last = attempts[-1]
        result_path = last / "RESULT.json"
        if not result_path.exists() and (last / "RAW_RESPONSE.json").exists():
            raw = _read(last / "RAW_RESPONSE.json")
            error = _read(last / "ERROR.json") if (last / "ERROR.json").exists() else None
            recovered = _parse(raw, chapter, stage["task_ids"], error)
            _write(result_path, recovered)
        if result_path.exists():
            result = _read(result_path)
            if result.get("complete") or not retry_failed:
                return {**public, "status": "complete" if result.get("complete") else "pending", "cache_hit": True,
                        "attempt_dir": str(last), "result": result, "result_sha256": _hash(result), "call_error": result.get("call_error"),
                        "usage": _read(last / "USAGE.json") if (last / "USAGE.json").exists() else None}
        elif not retry_failed:
            return {**public, "status": "uncertain_attempt", "attempt_dir": str(last),
                    "required_action": "Inspect the interrupted attempt/provider ledger, then use --retry-failed only if another charged call is intended."}
    if not run:
        return {**public, "status": "planned"}
    if client_factory is None:
        return {**public, "status": "blocked", "error": "client_factory_required_for_run"}
    attempt = stage_root / f"attempt_{len(attempts) + 1:03d}"
    attempt.mkdir(parents=True, exist_ok=False)
    _write(attempt / "MESSAGES.json", stage["messages"])
    _write(attempt / "REQUEST.json", {**public, "signature": {k: v for k, v in signature.items() if k != "messages"},
                                      "call_id": stage["stage_id"] + "-" + key[:12] + "-" + attempt.name})
    profile = stage["effective_profile"]
    call_error = None
    response: Any = None
    called = False
    try:
        client = client_factory(stage["role"], attempt, deepcopy(profile))
        _check_client(client, profile)
        # Revalidate against the factory's actual meter before dispatch. This
        # cannot cause a paid call or silently strip evidence if it disagrees.
        effective_counter = getattr(client, "prompt_token_counter", counter)
        actual_profile = {**profile}
        for name in ("prompt_token_multiplier", "prompt_token_framing_margin"):
            actual_profile[name] = getattr(client, name, profile[name])
        actual_estimate = _estimate(stage["messages"], actual_profile, max_input, effective_counter)
        _write(attempt / "ACTUAL_REQUEST.json", {"profile": actual_profile, "estimate": actual_estimate,
                                                 "wire_parameters": actual_estimate["wire_parameters"]})
        if not actual_estimate["fits"]:
            raise CandidateError("actual_client_meter_requires_batching:full_input_preserved")
        called = True
        response = invoke_client(client, stage["messages"], stage_id=stage["stage_id"], call_id=stage["stage_id"] + "-" + key[:12] + "-" + attempt.name,
                                 **{k: profile[k] for k in ("model", "thinking", "thinking_budget", "max_output_tokens",
                                                          "json_mode", "timeout_seconds", "stream", "stream_overall_timeout_seconds")})
    except Exception as exc:
        call_error = _safe_error(exc)
        response = call_error.get("record") or {"complete": False, "content": "", "error_type": type(exc).__name__}
        _write(attempt / "ERROR.json", call_error)
    # A raw response is persisted before parsing. A crash after this write is
    # recovered locally on resume without another provider call.
    _write(attempt / "RAW_RESPONSE.json", response)
    result = _parse(response, chapter, stage["task_ids"], call_error)
    usage = response.get("usage") if isinstance(response, Mapping) else None
    # Invocation is not evidence of a paid request. Known reservation/capacity
    # refusal happens before HTTP; unknown transport outcomes stay unknown.
    pre_dispatch_error = bool(call_error and any(code in call_error["error"] for code in
        ("global_budget_exceeded", "actual_client_meter_requires_batching", "client_profile_mismatch",
         "automatic_paid_retries", "reserved_attempt_cny_too_low", "missing_api_key", "credential")))
    paid_dispatch_count = (0 if not called or pre_dispatch_error or getattr(client_factory, "execution_mode", "injected") != "live"
                           else 1 if isinstance(response, Mapping) and response.get("request_id") else None)
    usage_record = {"usage": usage, "usage_known": bool(usage), "model": profile["model"],
                    "effective_request": response.get("effective_request") if isinstance(response, Mapping) else None,
                    "provider_request_id": response.get("request_id") if isinstance(response, Mapping) else None}
    if usage:
        try:
            usage_record["estimated_actual_cost_cny"] = estimated_cost_cny(usage, model=profile["model"])
        except Exception:
            usage_record["estimated_actual_cost_cny"] = None
    _write(attempt / "USAGE.json", usage_record)
    _write(attempt / "RESULT.json", result)
    _write(attempt / "BODY.md", result.get("body_markdown", ""), text=True)
    return {**public, "status": "complete" if result.get("complete") else "pending", "cache_hit": False,
            "attempt_dir": str(attempt), "result": result, "result_sha256": _hash(result),
            "model_calls": int(called), "client_invocations": int(called), "call_error": call_error,
            "paid_dispatch_count": paid_dispatch_count,
            "usage": usage_record}


def _qualified_blocks(result: Mapping[str, Any], stage_id: str, scope: Sequence[str] = ()) -> list[dict[str, Any]]:
    return [{**deepcopy(block), "block_id": f"{stage_id}::{block.get('block_id', index)}",
             "draft_scope_task_ids": list(scope) if not block.get("task_ids") else list(block["task_ids"])}
            for index, block in enumerate(result.get("blocks", []), 1)]


def required_editor_success(stages: Sequence[Mapping[str, Any]]) -> bool:
    return bool(stages) and all(stage["status"] == "complete" for stage in stages)


def run_candidate(chapter: dict[str, Any], *, route: str, output_dir: str | Path,
                  config: Mapping[str, Any], client_factory: Callable[..., Any] | None = None,
                  run: bool = False, retry_failed: bool = False,
                  fallback_hierarchical: bool = False, token_counter: Any = None) -> dict[str, Any]:
    """Plan or execute one explicit route and preserve every attempt/version.

    ``token_counter`` follows Qwen's (request_bytes, messages) contract. Budgets
    are reserved by the shared real client once each stage is actually called,
    never as a pessimistic whole-pipeline precondition.
    """
    output = Path(output_dir).resolve()
    with _output_lock(output):
        return _run_candidate(chapter, route=route, output=output, config=config,
                              client_factory=client_factory, run=run, retry_failed=retry_failed,
                              fallback_hierarchical=fallback_hierarchical, counter=token_counter)


def _run_candidate(chapter: dict[str, Any], *, route: str, output: Path,
                   config: Mapping[str, Any], client_factory: Any, run: bool,
                   retry_failed: bool, fallback_hierarchical: bool, counter: Any) -> dict[str, Any]:
    if route not in ROUTES:
        raise CandidateError(f"unknown_candidate_route:{route}")
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:10]
    run_dir = output / "runs" / run_id
    run_dir.mkdir(parents=True)
    input_hash = _hash(chapter)
    input_path = output / "inputs" / input_hash / "CHAPTER_INPUT.json"
    if not input_path.exists():
        _write(input_path, chapter)
    code_hash = _code_hash()
    manifest: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "run_id": run_id,
        "chapter_id": chapter.get("chapter_id"), "requested_route": route, "effective_route": route,
        "input_hash": input_hash, "input_path": str(input_path), "code_prompt_hash": code_hash,
        "source_file_hashes": _source_file_hashes(), "git_commit": _git_commit(),
        "prompt_file_hashes": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(PROMPT_ROOT.glob("*.md"))},
        "provenance": deepcopy(chapter.get("provenance", {})), "run": bool(run), "status": "planning",
        "semantic_quality_unreviewed": True, "material_preserved": True, "stages": [],
        "not_executed": [], "warnings": deepcopy(chapter.get("warnings", [])),
        "execution_mode": getattr(client_factory, "execution_mode", "injected" if client_factory else "preview")}

    def checkpoint():
        _write(run_dir / "RUN_MANIFEST.json", manifest)
        _write(output / "RUN_MANIFEST.json", manifest)

    checkpoint()
    draft_blocks: list[dict[str, Any]] = []
    selected_blocks: list[dict[str, Any]] = []
    try:
        config = validate_config(config)
        if route != "chapter" and not config["editor_pass"]:
            raise CandidateError("units_edit_and_hierarchical_require_editor_pass")
        manifest["config"] = deepcopy(config)
        writer = config["writer"]
        editor = config["editor"]
        max_input = config.get("max_input_tokens")
        if max_input is not None:
            if isinstance(max_input, bool) or int(max_input) != max_input or int(max_input) <= 0:
                raise CandidateError("max_input_tokens_must_be_positive_integer")
            max_input = int(max_input)
        if not isinstance(config.get("editor_pass", True), bool):
            raise CandidateError("editor_pass_must_be_boolean")
        catalog = task_catalog(chapter)
        manifest["material_manifest"] = _material_manifest(chapter, list(catalog))
        plan = _writer_plan(chapter, route, writer, max_input, counter)
        if fallback_hierarchical and route != "hierarchical" and any(not stage["estimate"]["fits"] for stage in plan):
            manifest["fallback_reason"] = "requested_route_input_exceeds_context"
            manifest["requested_route_capacity"] = [{"stage_id": p["stage_id"], "estimate": p["estimate"]} for p in plan]
            route = "hierarchical"
            manifest["effective_route"] = route
            plan = _writer_plan(chapter, route, writer, max_input, counter)
        manifest["profiles"] = {"writer": writer, "editor": editor}
        manifest["partition"] = [{"stage_id": p["stage_id"], "task_ids": p["task_ids"], "status": p["status"]} for p in plan]
        checkpoint()
        writer_stages = []
        draft_blocks = []
        for stage in plan:
            outcome = _execute_stage(stage, chapter=chapter, output=output, run_dir=run_dir, route=route,
                code_hash=code_hash, input_hash=input_hash, dependencies=[], client_factory=client_factory,
                run=run, retry_failed=retry_failed, max_input=max_input, counter=counter)
            writer_stages.append(outcome)
            manifest["stages"].append(outcome)
            draft_blocks.extend(_qualified_blocks(outcome.get("result", {}), stage["stage_id"], stage["task_ids"]))
            checkpoint()
        # The unchanged initial manuscript is always recoverable, independent
        # of which later edits are accepted and selected.
        draft_result = parse_candidate_response({"blocks": draft_blocks, "issues": []}, chapter)
        writers_complete = all(s["status"] == "complete" for s in writer_stages)
        draft_result["complete"] = bool(draft_result.get("complete") and writers_complete)
        _write(run_dir / "DRAFT_RESULT.json", draft_result)
        _write(run_dir / "DRAFT_BODY.md", render_blocks(draft_blocks), text=True)
        selected_blocks = deepcopy(draft_blocks)
        edit_stages: list[dict[str, Any]] = []
        editor_requested = route != "chapter" and config.get("editor_pass", True)
        writers_transport_complete = all(s.get("result", {}).get("transport_complete", False) for s in writer_stages)
        useful_draft = any(block.get("body_markdown", "").strip() for block in draft_blocks)
        if editor_requested and writers_transport_complete and useful_draft:
            # Coverage/table defects are repairable by the real editor. Supply
            # every missing approved task with its full evidence; never fill
            # its scientific answer in Python or make missing work disappear.
            editable_drafts = deepcopy(draft_blocks)
            covered_refs = {key for block in editable_drafts for key in block.get("task_ids", [])}
            for block in editable_drafts:
                block["pending_task_ids"] = [key for key in block.get("task_ids", []) if key in draft_result.get("pending_task_ids", [])]
            for key in catalog:
                if key not in covered_refs:
                    editable_drafts.append({"block_id": "pending::" + key, "task_ids": [key],
                        "body_markdown": "", "pending_task_ids": [key], "placeholder_for_unwritten_task": True})
            edits = _editor_plan(chapter, editable_drafts, route, editor, max_input, counter)
            deps = [{"stage_id": s["stage_id"], "cache_key": s["cache_key"], "result_sha256": s["result_sha256"]} for s in writer_stages]
            accepted: list[dict[str, Any]] = []
            for stage in edits:
                outcome = _execute_stage(stage, chapter=chapter, output=output, run_dir=run_dir, route=route,
                    code_hash=code_hash, input_hash=input_hash, dependencies=deps, client_factory=client_factory,
                    run=run, retry_failed=retry_failed, max_input=max_input, counter=counter)
                edit_stages.append(outcome)
                manifest["stages"].append(outcome)
                if outcome["status"] == "complete":
                    accepted.extend(_qualified_blocks(outcome["result"], stage["stage_id"]))
                else:
                    accepted.extend(deepcopy([b for b in editable_drafts[stage["block_start"]:stage["block_end"]] if not b.get("placeholder_for_unwritten_task")]))
                checkpoint()
            selected_blocks = accepted
        elif editor_requested:
            manifest["not_executed"].append({"role": "editor", "reason": "requires_useful_actual_drafts_and_complete_transport",
                "planned_behavior": "Try whole-chapter evidence-backed integration; if oversized, partition complete prose/task spans with full local evidence and fitting read-only neighbor blocks."})
        selected_stage_ids = {str(b.get("block_id", "")).split("::", 1)[0] for b in selected_blocks}
        issues = [issue for stage in manifest["stages"]
                  if stage["stage_id"] in selected_stage_ids or stage["status"] != "complete"
                  for issue in stage.get("result", {}).get("issues", [])
                  if stage["role"] == "editor" or not required_editor_success(edit_stages)]
        combined = parse_candidate_response({"blocks": selected_blocks, "issues": issues}, chapter)
        required_complete = (writers_complete if not editor_requested else writers_transport_complete and bool(edit_stages) and all(s["status"] == "complete" for s in edit_stages))
        complete = bool(combined.get("complete") and required_complete)
        status = "complete" if complete else ("preview" if not run else "pending")
        manifest["status"] = status
        manifest["model_calls"] = sum(s.get("model_calls", 0) for s in manifest["stages"])
        manifest["cached_stages"] = sum(bool(s.get("cache_hit")) for s in manifest["stages"])
        selected_stage_ids = {str(b.get("block_id", "")).split("::", 1)[0] for b in selected_blocks}
        manifest["selected_stage_lineage"] = [{"stage_id": s["stage_id"], "cache_key": s["cache_key"],
            "contributes_selected_blocks": s["stage_id"] in selected_stage_ids,
            "result_sha256": s.get("result_sha256"), "status": s["status"], "role": s["role"]} for s in manifest["stages"]]
        result = {**combined, "schema_version": SCHEMA_VERSION + ".result", "chapter_id": chapter.get("chapter_id"),
                  "run_id": run_id, "selected_version": run_id, "execution_mode": manifest["execution_mode"], "input_hash": input_hash, "requested_route": manifest["requested_route"],
                  "effective_route": route, "complete": complete, "status": status,
                  "semantic_quality_unreviewed": True, "selected_kind": ("edited" if edit_stages and all(s["status"] == "complete" for s in edit_stages) else
                      "mixed_partial_edits" if any(s["status"] == "complete" for s in edit_stages) else "draft"),
                  "editor_complete": not editor_requested or bool(edit_stages) and all(s["status"] == "complete" for s in edit_stages),
                  "stage_lineage": manifest["selected_stage_lineage"], "provenance": manifest["provenance"],
                  "output_dir": str(output)}
    except Exception as exc:
        manifest["status"] = "blocked"
        manifest["error"] = _safe_error(exc)
        result = {"schema_version": SCHEMA_VERSION + ".result", "chapter_id": chapter.get("chapter_id"),
                  "run_id": run_id, "selected_version": run_id, "execution_mode": manifest["execution_mode"], "input_hash": input_hash, "requested_route": manifest["requested_route"],
                  "effective_route": manifest["effective_route"], "status": "blocked", "complete": False,
                  "blocks": selected_blocks or draft_blocks, "body_markdown": render_blocks(selected_blocks or draft_blocks), "issues": [manifest["error"]],
                  "pending_task_ids": list(catalog) if "catalog" in locals() else [],
                  "semantic_quality_unreviewed": True, "selected_kind": "draft", "output_dir": str(output)}
    _write(run_dir / "CHAPTER_RESULT.json", result)
    _write(run_dir / "CHAPTER_BODY.md", result.get("body_markdown", ""), text=True)
    selected = result
    previous_path = output / "CHAPTER_RESULT.json"
    if previous_path.exists():
        try:
            previous = _read(previous_path)
            if previous.get("complete") and not result.get("complete"):
                selected = previous
                manifest["previous_complete_version_preserved"] = True
        except (ValueError, OSError):
            pass
    manifest["selected_version"] = selected["run_id"]
    manifest["current_run_version"] = run_id
    manifest["selected_input_matches_current"] = selected.get("input_hash") == input_hash
    manifest["selected_result_path"] = str(output / "runs" / selected["run_id"] / "CHAPTER_RESULT.json")
    if selected is result:
        _write(output / "CHAPTER_RESULT.json", result)
        _write(output / "CHAPTER_BODY.md", result.get("body_markdown", ""), text=True)
    checkpoint()
    report = {"schema_version": SCHEMA_VERSION + ".implementation_report", "run_id": run_id,
        "requested_route": manifest["requested_route"], "effective_route": manifest["effective_route"],
        "status": manifest["status"], "selected_version": manifest["selected_version"],
        "current_run_version": run_id, "selected_input_matches_current": manifest["selected_input_matches_current"],
        "selected_result_path": manifest["selected_result_path"], "partition": manifest.get("partition", []),
        "stages": [{k: v for k, v in stage.items() if k not in ("result",)} for stage in manifest["stages"]],
        "not_executed": manifest["not_executed"], "error": manifest.get("error"),
        "semantic_quality_unreviewed": True, "material_preserved": True,
        "verification_scope": "Execution, material provenance, task references, table syntax and existing citation diagnostics only; no claim of scientific or editorial quality.",
        "automatic_paid_retries": False, "budget_policy": "Shared client ledger reserves each actual stage separately; unknown usage is never reported as free.",
        "model_calls": manifest.get("model_calls", 0),
        "client_invocations": manifest.get("model_calls", 0),
        "model_calls_definition": "Client invocation attempts, including replay and pre-dispatch refusal; not a claim of paid provider dispatch.",
        "execution_mode": manifest["execution_mode"],
        "paid_dispatch_count": (None if any(s.get("paid_dispatch_count") is None for s in manifest["stages"])
                                else sum(s.get("paid_dispatch_count", 0) for s in manifest["stages"]))}
    _write(run_dir / "IMPLEMENTATION_REPORT.json", report)
    _write(output / "IMPLEMENTATION_REPORT.json", report)
    return {**result, "selected_version": manifest["selected_version"], "selected_result_path": manifest["selected_result_path"],
            "selected_input_matches_current": manifest["selected_input_matches_current"],
            "stages": report["stages"], "model_calls": manifest.get("model_calls", 0),
            "client_invocations": report["client_invocations"], "paid_dispatch_count": report["paid_dispatch_count"]}
