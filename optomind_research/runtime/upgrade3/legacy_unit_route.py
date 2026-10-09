"""Deterministic current-material adapter to the tested legacy unit writer.

No GUIDE conversion, planning model, manuscript seed, source clipping, or
previous-body context. Preview is offline. Live calls use the current writer
and its transport/budget checks; assembly is the existing production assembler.
"""
from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from . import review_unit_writer as writer
from .fullbody_contracts import _chapter_rows, _source_records, fullbody_task_catalog
from .module4.runtime import invoke_client, model_pricing
from .portable_paths import portable_component
from .writer_candidates import _output_lock
from .writer_candidates_contracts import CandidateError, _handles

SCHEMA = "optomind.legacy_unit_route.v1"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def build_unit_views(book: Mapping[str, Any]) -> list[writer.UnitWritingView]:
    """Keep chapter snapshots verbatim, with canonical identity and closure.

    Multiple snapshots of one source are retained in full, never overwritten
    by a book-level merged record. Only explicitly corroborated aliases from
    the existing fullbody identity validator are accepted.
    """
    chapters = _chapter_rows(book)
    if book.get("expected_chapter_ids") is not None and book["expected_chapter_ids"] != [c["chapter_id"] for c in chapters]:
        raise CandidateError("legacy_expected_chapter_sequence_mismatch")
    fullbody_task_catalog(book)  # Validate all unit/task IDs before any output.
    _, _, aliases = _source_records(chapters)
    if "source_aliases" in book and book["source_aliases"] != aliases:
        raise CandidateError("legacy_source_alias_map_mismatch")
    views = []
    for chapter in chapters:
        grouped = defaultdict(list)
        for original in chapter["sources"]:
            handle = original["source_handle"]
            grouped[aliases.get(handle, handle)].append(original)
        catalog = {}
        for handle, originals in grouped.items():
            material = deepcopy(originals[0])
            declared = [alias for alias, canonical in aliases.items() if canonical == handle]
            material["source_handle"] = handle
            material["aliases"] = declared
            # Exact originals remain inspectable even when identity fields need
            # normalization for the existing writer's alias validator.
            if len(originals) > 1:
                material["audit_complete_original_records"] = deepcopy(originals)
            elif material != originals[0]:
                material["original_source_identity"] = {key: deepcopy(originals[0][key])
                    for key in ("source_handle", "aliases") if key in originals[0]}
                material["original_source_identity_missing_fields"] = [key for key in
                    ("source_handle", "aliases") if key not in originals[0]]
            catalog[handle] = material
        writer._explicit_source_aliases(catalog)
        units = chapter["units"]
        for position, unit in enumerate(units, 1):
            tools = deepcopy(writer._unit_relevant_chapter_tool_materials(
                chapter.get("chapter_tool_materials") or [], unit["unit_id"]))
            pending = deque(_handles([unit, tools]))
            selected = []
            seen = set()
            while pending:
                supplied = pending.popleft()
                handle = aliases.get(supplied, supplied)
                if handle in seen:
                    continue
                if handle not in catalog:
                    raise CandidateError("legacy_unit_source_missing:" + chapter["chapter_id"]
                                         + ":" + unit["unit_id"] + ":" + supplied)
                seen.add(handle)
                selected.append(handle)
                pending.extend(_handles(catalog[handle]))
            views.append(writer.UnitWritingView(
                chapter_id=chapter["chapter_id"], unit_id=unit["unit_id"],
                focus=unit.get("focus", ""), unit_index=position, unit_count=len(units),
                sibling_units=[{"unit_id": sibling["unit_id"], "point": sibling.get("focus", "")}
                               for sibling in units if sibling["unit_id"] != unit["unit_id"]],
                chapter_frame=deepcopy(chapter.get("chapter_frame") or {}),
                other_chapters=deepcopy(chapter.get("other_chapters") or []),
                paragraph_tasks=deepcopy(unit.get("paragraph_tasks") or []),
                table_tasks=deepcopy(unit.get("table_tasks") or []),
                materials=[deepcopy(catalog[handle]) for handle in selected],
                sources={handle: deepcopy(catalog[handle]) for handle in selected},
                unit_notes=deepcopy(unit.get("unit_notes") or ""),
                owner_unit_context=deepcopy(unit.get("owner_unit_context") or {}),
                chapter_tool_materials=tools,
            ))
    return views


def build_legacy_payload(view: writer.UnitWritingView, *, language: str = "zh") -> dict[str, Any]:
    payload = writer.unit_payload(view, language=language, planning_revision=True)
    # The legacy projection knows a fixed set of task keys. Preserve today's
    # complete scientific fields (including future additions) without rewriting.
    payload["paragraph_tasks"] = deepcopy(view.paragraph_tasks)
    payload["table_tasks"] = deepcopy(view.table_tasks)
    return payload


def _profile(model: str, output_tokens: int, thinking_budget: int) -> dict[str, Any]:
    if model != "qwen3.5-plus":
        raise CandidateError("legacy_route_requires_explicit_qwen3.5-plus")
    pricing = model_pricing(model)
    if output_tokens < 64 or thinking_budget < 0 or output_tokens + thinking_budget > pricing["max_output_tokens"]:
        raise CandidateError("legacy_invalid_output_capacity")
    return {"model": model, "max_output_tokens": output_tokens,
            "thinking": bool(thinking_budget), "thinking_budget": thinking_budget,
            "json_mode": False, "timeout_seconds": 1800.0,
            "prompt_token_multiplier": 1.12, "prompt_token_framing_margin": 8192,
            "stream": True, "stream_overall_timeout_seconds": 3600.0}


def _code_hash() -> str:
    root = Path(__file__).parent
    return _hash({name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in
                  ("legacy_unit_route.py", "review_unit_writer.py", "fullbody_contracts.py",
                   "writer_candidates_contracts.py", "module4/runtime.py")})


class _CaptureClient:
    """Persist a response before parsing; uncertain errors never trigger retry."""
    def __init__(self, client: Any, attempt: Path, call_id: str):
        self.client, self.attempt, self.call_id = client, attempt, call_id
        self.invocations = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)

    def __call__(self, messages, **kwargs):
        self.invocations += 1
        try:
            response = invoke_client(self.client, messages, call_id=self.call_id, **kwargs)
        except Exception as exc:
            record = getattr(exc, "record", None)
            _write(self.attempt / "ERROR.json", {"type": type(exc).__name__,
                   "error": str(exc), "record": record})
            if isinstance(record, Mapping) and record.get("content"):
                _write(self.attempt / "RAW_RESPONSE.json", {**record, "complete": False})
            raise
        _write(self.attempt / "RAW_RESPONSE.json", response)
        return response


def _parse_raw(view, payload, profile, attempt, language, estimate, client=None, *, token_counter=None, mode="live"):
    if client is None:
        raw = _read(attempt / "RAW_RESPONSE.json")
        client = lambda messages, **kwargs: deepcopy(raw)
        client.prompt_token_counter = token_counter
    result = writer.run_unit_writing(
        view, client=client, model=profile["model"], payload=payload, language=language,
        planning_revision=True, output_tokens=profile["max_output_tokens"],
        thinking_budget=profile["thinking_budget"], thinking=profile["thinking"])
    complete = (bool(result.get("complete")) and bool(result.get("body_markdown", "").strip())
                and result.get("finish_reason") == "stop")
    saved = writer.write_unit_output(
        view, result["body_markdown"], attempt, model=profile["model"], language=language,
        mode="fake" if mode == "injected" else "run", used_messages=result["messages"], estimate=estimate,
        usage=result.get("usage"), response_path=str(attempt / "RAW_RESPONSE.json"),
        finish_reason=result.get("finish_reason", ""), complete=complete,
        partial_error=result.get("partial_error", ""), issues=result.get("issues", []),
        citation_diagnostics=result, effective_request=result.get("effective_request"),
        cap_pressure=result.get("cap_pressure"))
    saved["execution_mode"] = mode
    _write(attempt / "UNIT_RESULT.json", saved)
    _write(attempt / "RESULT_SEAL.json", {"sha256": _hash(saved),
           "body_file_sha256": hashlib.sha256(Path(saved["body_path"]).read_bytes()).hexdigest()})
    return saved


def run_legacy_units(book: Mapping[str, Any], *, output_dir: str | Path,
                     model: str = "qwen3.5-plus", output_tokens: int = 32768,
                     thinking_budget: int = 8192, budget_limit: float = 30.0,
                     run: bool = False, retry_failed: bool = False,
                     client_factory=None, token_counter=None) -> dict[str, Any]:
    """Preview all units or resume exact requests; never silently re-charge.

    An injected factory is an offline testing seam. Production callers must use
    the existing live factory with a dedicated persistent bounded ledger.
    """
    output = Path(output_dir).resolve()
    with _output_lock(output):
        return _run(book, output=output, model=model, output_tokens=output_tokens,
                    thinking_budget=thinking_budget, budget_limit=budget_limit, run=run,
                    retry_failed=retry_failed, client_factory=client_factory,
                    token_counter=token_counter)


def _run(book, *, output, model, output_tokens, thinking_budget, budget_limit,
         run, retry_failed, client_factory, token_counter):
    if isinstance(budget_limit, bool) or not math.isfinite(budget_limit) or budget_limit <= 0 or budget_limit > 30:
        raise CandidateError("legacy_budget_must_be_finite_positive_and_at_most_30_cny")
    profile = _profile(model, output_tokens, thinking_budget)
    views = build_unit_views(book)
    identity = {"schema_version": SCHEMA, "book_sha256": _hash(book), "profile": profile,
                "budget_limit": budget_limit, "code_sha256": _code_hash(),
                "prompt_sha256": _hash(writer.load_writer_prompt(planning_revision=True))}
    binding = output / "RUN_IDENTITY.json"
    if binding.exists() and _read(binding) != identity:
        raise CandidateError("legacy_output_identity_changed:use_a_new_output_directory")
    _write(binding, identity)
    _write(output / "FULL_BODY_INPUT.json", book)
    mode = getattr(client_factory, "execution_mode", "injected" if client_factory else "preview")
    execution_path = output / "EXECUTION_MODE.json"
    if not run and execution_path.exists():
        mode = _read(execution_path)["mode"]
    if run:
        if client_factory is None:
            raise CandidateError("legacy_run_requires_client_factory")
        execution_path = output / "EXECUTION_MODE.json"
        if execution_path.exists() and _read(execution_path) != {"mode": mode}:
            raise CandidateError("legacy_execution_mode_changed:use_a_new_output_directory")
        _write(execution_path, {"mode": mode})
    prepared = []
    for view in views:
        chapter = next(c for c in book["chapters"] if c["chapter_id"] == view.chapter_id)
        language = chapter.get("language") or book.get("language") or "zh"
        payload = build_legacy_payload(view, language=language)
        messages = writer.unit_messages(view, payload=payload, language=language, planning_revision=True)
        estimate = writer.estimate_unit_cost(messages, model=model, output_tokens=output_tokens,
                                              thinking_budget=thinking_budget, token_counter=token_counter)
        unit_root = output / "units" / portable_component(view.chapter_id) / portable_component(view.unit_id)
        key = _hash({"identity": identity, "messages": messages})
        stage = unit_root / key
        writer.write_unit_input(view, messages, stage, estimate=estimate, language=language)
        _write(stage / "UNIT_PACK.json", payload)
        prepared.append((view, payload, messages, estimate, stage, language))
    report = {"schema_version": SCHEMA, "execution_mode": mode, "original_units": len(views),
              "model": model, "budget_limit_cny": budget_limit,
              "estimated_all_units_cny": sum(row[3]["estimated_cost_cny"] for row in prepared),
              "material_preserved": True, "previous_body_context": False,
              "model_calls": 0, "units": [], "assembly": {}, "scientific_review_status": "not_run"}
    report["estimated_all_units_within_budget"] = report["estimated_all_units_cny"] <= budget_limit
    report["capacity_blocked_units"] = [f"{v.chapter_id}:{v.unit_id}" for v, p, m, e, s, l in prepared
                                       if e["input_capacity"]["exceeds_capacity"]]
    _write(output / "PREFLIGHT.json", report)
    jobs = []
    stop_new_calls = False
    for view, payload, messages, estimate, stage, language in prepared:
        row = {"chapter_id": view.chapter_id, "unit_id": view.unit_id,
               "messages_path": str(stage / "UNIT_MESSAGES.json"), "estimate": estimate,
               "status": "planned", "cache_hit": False}
        report["units"].append(row)
        if estimate["input_capacity"]["exceeds_capacity"]:
            row["status"] = "capacity_blocked"
            continue
        attempts = sorted(stage.glob("attempt_*"))
        chosen = None
        latest_partial = None
        for attempt in reversed(attempts):
            result_path = attempt / "UNIT_RESULT.json"
            if (not result_path.exists() or not (attempt / "RESULT_SEAL.json").exists()) and (attempt / "RAW_RESPONSE.json").exists():
                try:
                    _parse_raw(view, payload, profile, attempt, language, estimate, token_counter=token_counter, mode=mode)
                except Exception as exc:
                    row["recovery_error"] = type(exc).__name__ + ":" + str(exc)
            if result_path.exists():
                result = _read(result_path)
                seal = attempt / "RESULT_SEAL.json"
                if (not seal.exists() or _read(seal).get("sha256") != _hash(result)
                        or not Path(result["body_path"]).is_file()
                        or _read(seal).get("body_file_sha256") != hashlib.sha256(Path(result["body_path"]).read_bytes()).hexdigest()):
                    raise CandidateError("legacy_cached_result_integrity_failure:" + str(attempt))
                if latest_partial is None:
                    latest_partial = (attempt, result)
                if result.get("complete"):
                    chosen = (attempt, result)
                    break
        if chosen:
            row.update(status="complete", cache_hit=True)
        elif attempts and not retry_failed:
            row.update(status="pending_retry_approval", attempt_dir=str(attempts[-1]))
            chosen = latest_partial
        elif run and stop_new_calls:
            row["status"] = "not_started"
            chosen = latest_partial
        elif run:
            attempt = stage / f"attempt_{len(attempts) + 1:03d}"
            attempt.mkdir(parents=True, exist_ok=False)
            _write(attempt / "REQUEST.json", {"profile": profile, "messages_sha256": _hash(messages)})
            writer.write_unit_input(view, messages, attempt, estimate=estimate, language=language)
            captured = None
            try:
                client = client_factory("writer", attempt, deepcopy(profile))
                if getattr(client, "max_retries", 0) != 0:
                    raise CandidateError("legacy_automatic_paid_retries_forbidden")
                for field in ("model", "max_output_tokens", "thinking", "thinking_budget"):
                    if hasattr(client, field) and getattr(client, field) != profile[field]:
                        raise CandidateError("legacy_client_profile_mismatch:" + field)
                captured = _CaptureClient(client, attempt, stage.name[:16] + "-" + attempt.name)
                result = _parse_raw(view, payload, profile, attempt, language, estimate, captured,
                                    token_counter=token_counter, mode=mode)
                row["status"] = "complete" if result.get("complete") else "pending_retry_approval"
                chosen = (attempt, result)
                report["model_calls"] += captured.invocations
            except Exception as exc:
                report["model_calls"] += captured.invocations if captured is not None else 0
                row.update(status="blocked", error=type(exc).__name__ + ":" + str(exc))
                _write(attempt / "RUN_ERROR.json", {"error": row["error"]})
                # Stop new calls, but keep scanning later exact caches. A
                # failed replacement must not discard earlier usable prose.
                stop_new_calls = True
                chosen = latest_partial
                row["failed_attempt_dir"] = str(attempt)
                report.setdefault("stopped_after", f"{view.chapter_id}:{view.unit_id}")
                _write(output / "RUN_REPORT.json", report)
        if chosen:
            attempt, result = chosen
            row["attempt_dir"] = str(attempt)
            row["pending_problems"] = [*result.get("issues", []), *result.get("citation_problems", [])]
            if result.get("body_markdown", "").strip():
                jobs.append({"chapter_id": view.chapter_id, "unit_id": view.unit_id,
                             "output": str(attempt), "arrangement": ""})
        _write(output / "RUN_REPORT.json", report)
    recorded_ids = {(row["chapter_id"], row["unit_id"]) for row in report["units"]}
    for view in views:
        if (view.chapter_id, view.unit_id) not in recorded_ids:
            report["units"].append({"chapter_id": view.chapter_id, "unit_id": view.unit_id, "status": "not_started"})
    report["complete_units"] = sum(row["status"] == "complete" for row in report["units"])
    report["missing_units"] = [f"{v.chapter_id}:{v.unit_id}" for v in views
                               if not any(j["chapter_id"] == v.chapter_id and j["unit_id"] == v.unit_id for j in jobs)]
    # Never reuse a preexisting assembly after an input/profile change. The
    # output root is identity-bound above, and only this run's results enter.
    if jobs and mode != "injected":
        batch = output / "batch"
        manifest = {"review_title": "文献综述", "chapters": []}
        for chapter in book["chapters"]:
            catalog = {}
            for view in views:
                if view.chapter_id == chapter["chapter_id"]:
                    catalog.update(view.sources)
            frame = chapter.get("chapter_frame") or {}
            arrangement = {"chapter_id": chapter["chapter_id"], "units": deepcopy(chapter["units"]),
                           "chapter_argument": frame.get("chapter_argument", ""),
                           "title": frame.get("chapter_title") or frame.get("title") or chapter["chapter_id"],
                           "source_catalog": catalog}
            path = batch / portable_component(chapter["chapter_id"]) / "CHAPTER_ARRANGEMENT.json"
            _write(path, arrangement)
            manifest["chapters"].append({"chapter_id": chapter["chapter_id"], "arrangement_path": str(path)})
            for job in jobs:
                if job["chapter_id"] == chapter["chapter_id"]:
                    job["arrangement"] = str(path)
        # Include unavailable jobs explicitly. The assembler's fallback paths
        # belong to an old historical default directory and must never be used.
        all_jobs = list(jobs)
        for view, payload, messages, estimate, stage, language in prepared:
            if not any(j["chapter_id"] == view.chapter_id and j["unit_id"] == view.unit_id for j in jobs):
                arrangement = next(c["arrangement_path"] for c in manifest["chapters"] if c["chapter_id"] == view.chapter_id)
                all_jobs.append({"chapter_id": view.chapter_id, "unit_id": view.unit_id,
                                 "output": str(stage / "not_written"), "arrangement": arrangement})
        _write(batch / "BATCH_JOBS.json", all_jobs)
        _write(batch / "MANIFEST.json", manifest)
        from .review_delivery import _assemble
        assembly_batch, assembly_manifest = batch, batch / "MANIFEST.json"
        if report["missing_units"]:
            # The existing assembler refuses missing units even in partial
            # mode. Filter by chapter+unit identity, then restore original
            # accounting and a visible restricted-draft label, as the existing
            # delivery wrapper does (without its globally-unique-unit-ID assumption).
            assembly_batch = output / "batch_filtered"
            filtered_manifest = {**manifest, "chapters": []}
            keep = {(job["chapter_id"], job["unit_id"]) for job in jobs}
            filtered_jobs = []
            for chapter in manifest["chapters"]:
                arrangement = _read(Path(chapter["arrangement_path"]))
                arrangement["units"] = [unit for unit in arrangement["units"]
                    if (chapter["chapter_id"], unit["unit_id"]) in keep]
                path = assembly_batch / portable_component(chapter["chapter_id"]) / "CHAPTER_ARRANGEMENT.json"
                _write(path, arrangement)
                filtered_manifest["chapters"].append({**chapter, "arrangement_path": str(path)})
                filtered_jobs.extend({**job, "arrangement": str(path)} for job in jobs
                                     if job["chapter_id"] == chapter["chapter_id"])
            assembly_manifest = assembly_batch / "MANIFEST.json"
            _write(assembly_manifest, filtered_manifest)
            _write(assembly_batch / "BATCH_JOBS.json", filtered_jobs)
        report["assembly"] = _assemble(assembly_batch, assembly_manifest, output / "assembled", allow_partial=False)
        if not report["assembly"]:
            raise CandidateError("legacy_assembly_failed:inspect_saved_unit_results")
        if report["missing_units"]:
            from scripts.upgrade3 import full_review_draft as draft
            summary = report["assembly"]
            summary.update(status="partial_check", original_units=len(views), expected_units=len(views),
                           manifest_units=len(views), missing_units=report["missing_units"], problems_resolved=False)
            summary.setdefault("pending_problems", []).extend(
                {"unit_id": key, "code": "missing_unit"} for key in report["missing_units"])
            _write(output / "assembled" / "ASSEMBLY_SUMMARY.json", summary)
            (output / "assembled" / "RUN_REPORT.md").write_text(draft._report_markdown(summary), encoding="utf-8")
            for filename in ("REVIEW_DRAFT.md", "REVIEW_DRAFT_HANDLES.md"):
                path = output / "assembled" / filename
                content = path.read_text(encoding="utf-8")
                banner = ("RESTRICTED PARTIAL DRAFT: " + str(len(jobs)) + "/" + str(len(views))
                          + " planned units. Missing: " + ", ".join(report["missing_units"]) + ".\n\n")
                path.write_text(banner + content, encoding="utf-8")
    report["generation_complete"] = report["complete_units"] == len(views)
    report["assembly_complete"] = bool(report["assembly"] and
        report["assembly"].get("loaded_units") == len(views) and
        not report["assembly"].get("pending_problems"))
    structural_pending = any(row.get("pending_problems") for row in report["units"])
    report["status"] = ("simulated" if report["generation_complete"] and mode == "injected" else
                        "restricted_draft" if report["generation_complete"] and structural_pending else
                        "written_pending_review" if report["generation_complete"] else
                        "preview" if not run else "pending")
    if hasattr(client_factory, "ledger_snapshot"):
        report["ledger"] = client_factory.ledger_snapshot()
        if report["ledger"]:
            report["unsettled_budget_holds"] = sum(row["status"] in {"reserved", "uncertain"}
                for row in report["ledger"].get("reservations", []))
    _write(output / "RUN_REPORT.json", report)
    return report
