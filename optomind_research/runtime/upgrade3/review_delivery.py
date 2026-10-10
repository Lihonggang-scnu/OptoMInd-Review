"""Unified offline delivery entry for review-v2.

Three start points, one assembly backend (the deterministic assembler in
``scripts/upgrade3/full_review_draft.py``):

- ``history``: import an existing written manuscript from its delivery
  manifest + batch artifacts (unit results, reused cross-directory results,
  front/back matter, title/table overrides) and re-assemble it.  No model
  response is needed for the import itself.
- ``plan``: start from a writer packet, run the frozen planning_revision
  chain (build_chapter_view -> arrangement -> build_unit_view/unit_messages
  -> parse -> assemble) with REPLAYED recorded responses.  A missing
  recording leaves that step ``pending`` — never a live fallback.  This
  module never constructs or imports a live model client.

``body`` consumes the current complete FULL_BODY_INPUT through the existing
resumable unit route. Its caller supplies the same owned live factory; preview
and the history/plan branches remain offline.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

from .chapter_arrangement import (
    DEFAULT_OUTPUT_TOKENS as ARRANGEMENT_OUTPUT_TOKENS,
    DEFAULT_THINKING_BUDGET as ARRANGEMENT_THINKING_BUDGET,
    arrangement_messages,
    build_chapter_view,
    build_source_catalog,
    compact_chapter_tool_materials,
    load_editor_prompt,
    render_arrangement_markdown,
    run_arrangement,
    write_view,
)
from .review_unit_writer import (
    DEFAULT_OUTPUT_TOKENS as WRITER_OUTPUT_TOKENS,
    DEFAULT_THINKING_BUDGET as WRITER_THINKING_BUDGET,
    build_unit_view,
    load_writer_prompt,
    run_unit_writing,
    unit_messages,
    unit_payload,
    write_unit_input,
    write_unit_output,
    estimate_unit_cost,
)

LIVE_CLIENT_FACTORY = None  # documented unused seam: live is out of scope here
BODY_DELIVERY_DEFAULTS = {"body_version": "baseline", "quality_control": True, "article_edit": True}

REPLAY_EXACT = "exact"
REPLAY_COMPATIBILITY = "compatibility_replay"
PENDING_MISSING_RECORDING = "pending_missing_recording"


class MissingRecording(Exception):
    """Raised internally when a replay key has no recorded response."""


class ReplayClient:
    """Callable stand-in mirroring the injected-client protocol.

    Returns the recorded flat response for the key the runtime is about to
    request.  Unknown keys raise :class:`MissingRecording`; the runtime turns
    that into a pending step.  There is deliberately no live fallback.
    """

    def __init__(self, recordings: Mapping[str, Mapping[str, Any]]):
        self._recordings = dict(recordings)
        self._key = ""
        self._messages_blob = ""
        self.consumed: list[str] = []
        self.modes: dict[str, str] = {}

    def next(self, key: str, messages: Sequence[Mapping[str, Any]]) -> None:
        self._key = key
        self._messages_blob = json.dumps(
            [dict(m) for m in messages], ensure_ascii=False, sort_keys=True)

    def _sha(self) -> str:
        return hashlib.sha256(self._messages_blob.encode("utf-8")).hexdigest()

    def __call__(self, messages, **kwargs):  # noqa: ANN001 - mirrors client protocol
        entry = self._recordings.get(self._key)
        if entry is None:
            raise MissingRecording(self._key)
        recorded_sha = str(entry.get("messages_sha256") or "")
        if recorded_sha and recorded_sha == self._sha():
            self.modes[self._key] = REPLAY_EXACT
        else:
            self.modes[self._key] = REPLAY_COMPATIBILITY
        self.consumed.append(self._key)
        response = dict(entry.get("response") or {})
        response.setdefault("model", entry.get("model") or "")
        return response


def load_recordings(path: str | Path) -> dict[str, dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    recordings = data.get("recordings") if isinstance(data, Mapping) and isinstance(data.get("recordings"), Mapping) else data
    if not isinstance(recordings, Mapping):
        raise ValueError("recordings_must_be_object")
    return dict(recordings)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")


def _delivery_run_report(base_report: str, *, original_units: int,
                         assembled_units: int, missing_units: Sequence[str]) -> str:
    """Prepend the delivery-level accounting to the assembler's run report.

    The assembler only knows the (possibly filtered) unit list, so its own
    “预计/已载入/缺少” lines can read as complete when the delivery filtered
    units away.  The delivery header states the original planned size, what
    actually entered the manuscript, and the missing list explicitly.
    """

    lines = [
        "# 全稿汇编运行报告",
        "",
        f"- 交付口径：原计划 {original_units} 个单元；实际装配 {assembled_units} 个；"
        f"缺件 {len(missing_units)} 个"
        + (f"（{', '.join(missing_units)}）" if missing_units else ""),
        "",
        base_report,
    ]
    return "\n".join(lines)


def _assemble(batch_root: Path, manifest_path: Path, output_root: Path,
              allow_partial: bool = True) -> dict[str, Any]:
    """Run the production assembler CLI with explicit paths."""
    from scripts.upgrade3 import full_review_draft as draft

    argv = ["--manifest", str(manifest_path), "--batch-root", str(batch_root),
            "--output-root", str(output_root)]
    if allow_partial:
        argv.append("--allow-partial")
    draft.main(argv)
    summary_path = output_root / "ASSEMBLY_SUMMARY.json"
    return json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}


def run_history_delivery(
    *,
    manifest_path: str | Path,
    batch_root: str | Path,
    out_dir: str | Path,
    language: str = "zh",
) -> dict[str, Any]:
    """Import a written manuscript and re-assemble it offline.

    Units whose results cannot be resolved are reported as missing and left
    out of the assembled restricted draft — the remaining usable content is
    still assembled instead of failing the whole delivery.
    """
    from scripts.upgrade3 import full_review_draft as draft

    manifest_path = Path(manifest_path).resolve()
    batch_root = Path(batch_root).resolve()
    out_dir = Path(out_dir).resolve()
    if not manifest_path.is_file():
        raise FileNotFoundError(f"delivery manifest not found: {manifest_path}")

    manifest, fallback_jobs, _arrangements = draft.load_manifest(manifest_path)
    jobs = draft.load_jobs(batch_root, fallback_jobs)
    batch_results = draft._batch_run_paths(batch_root)
    missing = [job.unit_id for job in jobs
               if draft.resolve_result(job, batch_results) is None]

    assembly_manifest = manifest_path
    assembly_batch = batch_root
    restricted = False
    if missing:
        restricted = True
        filtered_root = out_dir / "batch_filtered"
        filtered_root.mkdir(parents=True, exist_ok=True)
        keep = {job.unit_id for job in jobs if job.unit_id not in set(missing)}
        filtered_jobs = []
        filtered_chapters = []
        for chapter in manifest.get("chapters") or []:
            chapter_id = str(chapter.get("chapter_id") or "")
            arrangement = json.loads(Path(str(chapter.get("arrangement_path"))).read_text(encoding="utf-8"))
            arrangement = dict(arrangement)
            arrangement["units"] = [u for u in (arrangement.get("units") or [])
                                    if str(u.get("unit_id") or "") in keep]
            filtered_arrangement_path = filtered_root / f"{chapter_id}.json"
            _write_json(filtered_arrangement_path, arrangement)
            filtered_chapters.append({**dict(chapter),
                                       "arrangement_path": str(filtered_arrangement_path)})
            for job in jobs:
                if job.chapter_id == chapter_id and job.unit_id in keep:
                    filtered_jobs.append({
                        "chapter_id": job.chapter_id, "unit_id": job.unit_id,
                        "arrangement": str(filtered_arrangement_path),
                        "output": str(job.output),
                        **({"reused_result": str(job.reused_result)}
                           if job.reused_result else {}),
                    })
        _write_json(filtered_root / "BATCH_JOBS.json", filtered_jobs)
        # BATCH_RUN.json may be the only locator for a unit (no `output`
        # default, no `reused_result`).  Copy it so resolve_result keeps
        # working inside the filtered batch root.
        for name in ("BATCH_RUN.json", "FRONT_MATTER.md", "BACK_MATTER.md",
                     "TITLE_OVERRIDES.json", "TABLE_TITLES.json"):
            source = batch_root / name
            if source.is_file():
                (filtered_root / name).write_text(source.read_text(encoding="utf-8"),
                                                  encoding="utf-8")
        filtered_manifest = {**dict(manifest), "chapters": filtered_chapters}
        assembly_manifest = filtered_root / "MANIFEST.json"
        _write_json(assembly_manifest, filtered_manifest)
        assembly_batch = filtered_root

    summary = _assemble(assembly_batch, assembly_manifest, out_dir / "assembled")
    if missing:
        summary = dict(summary)
        summary["status"] = "partial_check"
        summary["original_units"] = len(jobs)
        summary["missing_units"] = list(missing)
        summary.setdefault("pending_problems", []).extend(
            {"unit_id": unit_id, "code": "missing_unit"} for unit_id in missing)
        summary["problems_resolved"] = False
        # Rewrite the persisted summary and run report so DELIVERY_REPORT,
        # ASSEMBLY_SUMMARY and RUN_REPORT all state the same restricted
        # status, with the delivery-level original/assembled/missing counts.
        (out_dir / "assembled" / "ASSEMBLY_SUMMARY.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        (out_dir / "assembled" / "RUN_REPORT.md").write_text(
            _delivery_run_report(draft._report_markdown(summary),
                                 original_units=len(jobs),
                                 assembled_units=summary.get("loaded_units", 0),
                                 missing_units=list(missing)),
            encoding="utf-8")
    report = {
        "start": "history",
        "manifest": str(manifest_path),
        "batch_root": str(batch_root),
        "output_root": str(out_dir / "assembled"),
        "restricted_import": restricted,
        # Original plan size vs what actually entered the assembly.  A
        # filtered 3/3 must never read as the full planned manuscript.
        "original_units": len(jobs),
        "assembled_units": summary.get("loaded_units", 0),
        "missing_units": list(missing),
        "assembly": summary,
        "model_calls": 0,
        "external_requests": 0,
        "pending": [],
    }
    _write_json(out_dir / "DELIVERY_REPORT.json", report)
    return report


def run_plan_delivery(
    *,
    packet_path: str | Path,
    recordings_path: str | Path,
    out_dir: str | Path,
    language: str = "zh",
    models: Mapping[str, str] | None = None,
    arrangement_output_tokens: int = ARRANGEMENT_OUTPUT_TOKENS,
    arrangement_thinking_budget: int = ARRANGEMENT_THINKING_BUDGET,
    writer_output_tokens: int = WRITER_OUTPUT_TOKENS,
    writer_thinking_budget: int = WRITER_THINKING_BUDGET,
) -> dict[str, Any]:
    """Frozen planning_revision chain with replayed responses, then assembly."""
    packet_path = Path(packet_path).resolve()
    out_dir = Path(out_dir).resolve()
    models = dict(models or {})
    recordings = load_recordings(recordings_path)
    replay = ReplayClient(recordings)
    pending: list[dict[str, str]] = []

    view = build_chapter_view(packet_path, shared_outline=[], review_argument="",
                              id_map_path=out_dir / "ID_MAP.json")
    chapter_id = view.chapter_id
    arrangement_dir = out_dir / "arrangement" / chapter_id
    write_view(view, arrangement_dir / "ARRANGEMENT_INPUT.json")

    payload = view.arrangement_payload()
    payload["planning_revision_mode"] = True
    prompt = load_editor_prompt(planning_revision=True)
    messages = arrangement_messages(payload, prompt=prompt, planning_revision=True)
    _write_json(out_dir / "messages" / "arrangement_messages.json", messages)
    key = f"arrangement:{chapter_id}"
    replay.next(key, messages)
    try:
        arrangement = run_arrangement(
            view, client=replay, model=models.get("arrangement", "qwen3.5-plus"),
            prompt=prompt, view_payload=payload, call_id=f"delivery-replay:{key}",
            output_tokens=arrangement_output_tokens, thinking_budget=arrangement_thinking_budget,
            raw_response_dir=out_dir / "raw_responses", planning_revision=True)
    except MissingRecording:
        pending.append({"step": key, "status": PENDING_MISSING_RECORDING})
        report = {
            "start": "plan", "packet": str(packet_path), "output_root": str(out_dir),
            "replay_modes": replay.modes, "pending": pending,
            "written_units": [],
            "assembly": {}, "model_calls": 0, "external_requests": 0,
            "note": "arrangement recording missing; packet imported, chain stopped before model-dependent steps",
        }
        _write_json(out_dir / "DELIVERY_REPORT.json", report)
        return report
    validation = arrangement.get("validation") or {}
    if not validation.get("ok"):
        pending.append({"step": key, "status": "arrangement_validation_failed",
                        "errors": [str(e) for e in validation.get("errors") or []]})
        report = {
            "start": "plan", "packet": str(packet_path), "output_root": str(out_dir),
            "replay_modes": replay.modes, "pending": pending,
            "written_units": [],
            "assembly": {}, "model_calls": 0, "external_requests": 0,
            "note": "replayed arrangement failed contract validation; nothing assembled",
        }
        _write_json(out_dir / "DELIVERY_REPORT.json", report)
        return report

    exported = dict(arrangement)
    exported["source_catalog"] = build_source_catalog(view, arrangement)
    exported["chapter_tool_materials"] = compact_chapter_tool_materials(view)
    arrangement_path = arrangement_dir / "CHAPTER_ARRANGEMENT.json"
    _write_json(arrangement_path, exported)
    (arrangement_dir / "CHAPTER_ARRANGEMENT.md").write_text(
        render_arrangement_markdown(exported, view), encoding="utf-8", newline="\n")

    writer_prompt = load_writer_prompt(planning_revision=True)
    original_units = len(exported.get("units") or [])
    jobs = []
    for unit in arrangement.get("units") or []:
        unit_id = str(unit.get("unit_id") or "")
        if not unit_id:
            continue
        unit_dir = out_dir / "writer" / unit_id
        wview = build_unit_view(arrangement_path, unit_id)
        unit_payload_dict = unit_payload(wview, language=language)
        unit_payload_dict["planning_revision_mode"] = True
        unit_messages_list = unit_messages(wview, prompt=writer_prompt, language=language,
                                           payload=unit_payload_dict, planning_revision=True)
        _write_json(out_dir / "messages" / f"{unit_id}_messages.json", unit_messages_list)
        estimate = estimate_unit_cost(unit_messages_list, model=models.get("writer", "qwen3.7-flash"),
                                      output_tokens=writer_output_tokens, thinking_budget=writer_thinking_budget,
                                      token_counter=None)
        write_unit_input(wview, unit_messages_list, unit_dir, estimate=estimate, language=language)

        sha = hashlib.sha256(json.dumps(
            [dict(m) for m in unit_messages_list], ensure_ascii=False,
            sort_keys=True).encode("utf-8")).hexdigest()
        result_path = unit_dir / "UNIT_RESULT.json"
        sha_path = unit_dir / "INPUT_MESSAGES_SHA256.txt"
        unchanged_input = (
            result_path.is_file() and sha_path.is_file()
            and sha_path.read_text(encoding="utf-8").strip() == sha
        )
        existing_complete = False
        if unchanged_input:
            try:
                existing_complete = json.loads(
                    result_path.read_text(encoding="utf-8")).get("complete") is True
            except (ValueError, OSError):
                existing_complete = False
        if unchanged_input and existing_complete:
            jobs.append({"chapter_id": chapter_id, "unit_id": unit_id,
                         "arrangement": str(arrangement_path), "output": str(unit_dir)})
            continue  # unchanged input with a complete result: reuse it
        unit_key = f"writer:{unit_id}"
        replay.next(unit_key, unit_messages_list)
        try:
            result = run_unit_writing(
                wview, client=replay, model=models.get("writer", "qwen3.7-flash"),
                prompt=writer_prompt, language=language, payload=unit_payload_dict,
                output_tokens=writer_output_tokens, thinking_budget=writer_thinking_budget,
                raw_response_dir=unit_dir / "raw_responses", planning_revision=True)
        except MissingRecording:
            if unchanged_input:
                # Same input, existing body (possibly a partial one): keep it
                # in the restricted draft and say a complete recording is
                # still awaited — never silently call the old body final.
                jobs.append({"chapter_id": chapter_id, "unit_id": unit_id,
                             "arrangement": str(arrangement_path), "output": str(unit_dir)})
                pending.append({"step": unit_key, "status": "kept_incomplete_result"})
            else:
                # The input changed (or no prior result): the stale body must
                # not enter the manuscript under any circumstance.
                pending.append({"step": unit_key, "status": PENDING_MISSING_RECORDING})
            continue
        write_unit_output(
            wview, result["body_markdown"], unit_dir,
            model=models.get("writer", "qwen3.7-flash"), language=language, mode="run",
            used_messages=result["messages"], estimate=estimate,
            usage=result.get("usage") or {}, response_path=result.get("raw_response") or "",
            finish_reason=result.get("finish_reason") or "",
            complete=result.get("complete", True),
            partial_error=result.get("partial_error") or "",
            issues=result.get("issues") or [],
            effective_request=result.get("effective_request"), cap_pressure=result.get("cap_pressure"))
        sha_path.write_text(sha, encoding="utf-8")
        jobs.append({"chapter_id": chapter_id, "unit_id": unit_id,
                     "arrangement": str(arrangement_path), "output": str(unit_dir)})

    batch_root = out_dir / "batch"
    written_ids = {job["unit_id"] for job in jobs}
    # The production assembler refuses a manifest whose units are missing by
    # design.  When some units are pending (no recording), the delivery
    # filters the arrangement to the written units so the usable content is
    # still assembled as a restricted draft, honestly reported.
    assembly_arrangement_path = arrangement_path
    if pending and written_ids:
        filtered_arrangement = dict(exported)
        filtered_arrangement["units"] = [
            unit for unit in (exported.get("units") or [])
            if str(unit.get("unit_id") or "") in written_ids
        ]
        assembly_arrangement_path = batch_root / "ARRANGEMENT_FILTERED.json"
        _write_json(assembly_arrangement_path, filtered_arrangement)
    _write_json(batch_root / "BATCH_JOBS.json", jobs)
    manifest = {
        "review_title": str(view.title or packet_path.stem),
        "chapters": [{"chapter_id": chapter_id,
                      "arrangement_path": str(assembly_arrangement_path)}],
    }
    _write_json(batch_root / "MANIFEST.json", manifest)
    assembly: dict[str, Any] = {}
    if jobs:
        assembly = _assemble(batch_root, batch_root / "MANIFEST.json", out_dir / "assembled")
        if pending:
            assembly = dict(assembly)
            assembly["status"] = "partial_check"
            assembly["original_units"] = original_units
            missing_unit_ids = [item.get("step", "").split(":", 1)[-1]
                                for item in pending
                                if item.get("status") == PENDING_MISSING_RECORDING]
            assembly["missing_units"] = missing_unit_ids
            problems = list(assembly.get("pending_problems") or [])
            problems.extend(
                {"unit_id": item.get("step", "").split(":", 1)[-1],
                 "code": item.get("status")}
                for item in pending)
            assembly["pending_problems"] = problems
            assembly["problems_resolved"] = False
            # Keep the persisted summary and run report consistent with the
            # patched assembly state, with delivery-level unit accounting.
            from scripts.upgrade3 import full_review_draft as draft
            (out_dir / "assembled" / "ASSEMBLY_SUMMARY.json").write_text(
                json.dumps(assembly, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
            (out_dir / "assembled" / "RUN_REPORT.md").write_text(
                _delivery_run_report(draft._report_markdown(assembly),
                                     original_units=original_units,
                                     assembled_units=assembly.get("loaded_units", 0),
                                     missing_units=missing_unit_ids),
                encoding="utf-8")
    report = {
        "start": "plan", "packet": str(packet_path), "output_root": str(out_dir),
        "replay_modes": replay.modes, "pending": pending,
        "written_units": [job["unit_id"] for job in jobs],
        # Original plan size vs what actually entered the assembly; a filtered
        # 3/3 must never read as the full planned manuscript.
        "original_units": original_units,
        "assembled_units": assembly.get("loaded_units", 0) if assembly else 0,
        "missing_units": [item.get("step", "").split(":", 1)[-1] for item in pending
                          if item.get("status") == PENDING_MISSING_RECORDING],
        "assembly": assembly, "model_calls": 0, "external_requests": 0,
    }
    _write_json(out_dir / "DELIVERY_REPORT.json", report)
    return report


DELIVERY_CONFIG_SCHEMA = "review_v2_delivery.config.v1"


class DeliveryConfigError(ValueError):
    """Malformed --delivery-config: missing file, bad schema, unreadable field.

    Raised loudly (CLI exits non-zero).  A VALID config that simply omits a
    stage input is not an error — that stage reports ``pending`` instead, so
    a chain can never silently degrade to assembly-only while claiming a full
    downstream run.
    """


def load_post_body_context(
    value: Any = None, *, base_dir: str | Path = ".",
) -> dict[str, Any]:
    """Load optional post-BODY context without importing a planner or model.

    The context can be an inline object or a JSON file reference (a path or
    ``{"path": "context.json"}``).  ``final_outline``, ``material_records``
    and ``source_identity_map`` also accept JSON file references.  Every path
    resolves against the caller's explicit base directory, never the process
    working directory.  No conception card or early PartPlan is required.
    """
    base_dir = Path(base_dir).resolve()

    def read_reference(entry: Any, field: str) -> Any:
        reference = None
        if isinstance(entry, (str, Path)):
            reference = entry
        elif isinstance(entry, Mapping) and set(entry) == {"path"}:
            reference = entry["path"]
        if reference is None:
            return entry
        if not isinstance(reference, (str, Path)) or not str(reference).strip():
            raise DeliveryConfigError(f"post_body_context_bad_path:{field}")
        path = Path(reference)
        if not path.is_absolute():
            path = base_dir / path
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise DeliveryConfigError(
                f"post_body_context_unreadable:{field}:{path}:{type(exc).__name__}"
            ) from exc

    if value is None:
        return {}
    value = read_reference(value, "context")
    if not isinstance(value, Mapping):
        raise DeliveryConfigError("post_body_context_not_object")
    context = dict(value)
    for field in ("final_outline", "material_records", "source_identity_map"):
        if field in context:
            context[field] = read_reference(context[field], field)
    if "material_records" in context and not isinstance(context["material_records"], list):
        raise DeliveryConfigError("post_body_context_material_records_not_list")
    return context


def load_delivery_config(path: str | Path) -> dict[str, Any]:
    """Load and validate the downstream delivery config.

    Schema ``review_v2_delivery.config.v1``::

        {
          "schema": "review_v2_delivery.config.v1",
          "language": "zh",
          "research_question": "…",                  # 03 needs it
          "chapter_roles": [{"chapter_id": "…", "title": "…", "role": "…"}],
          "text_edit":   {"fixture": "EDIT_FIXTURE.json"},   # or {"recordings": …}
          "front_back":  {"fixture": "PARTS_FIXTURE.json"},  # or {"recordings": …}
          # Opt in only: front_back.mode="post_body" uses conception first.
          "post_body_context": {"shared_scope": "…", "final_outline": []},
          "identity_catalogs": {"path": "IDENTITY_CATALOGS.json"},  # or inline list
          "figure_assets": {"path": "FIGURE_ASSETS.json"},          # or inline list
          "table_moves": {}, "figure_moves": {},     # optional {"3": 5}
          "compile_pdf": false
        }

    ``{"path": …}`` entries resolve relative to the config file's directory.
    Every model result comes from a labeled fixture or an existing recording;
    there is no live route in this schema.
    """

    config_path = Path(path).resolve()
    if not config_path.is_file():
        raise DeliveryConfigError(f"delivery_config_not_found:{config_path}")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise DeliveryConfigError("delivery_config_not_object")
    if str(data.get("schema") or "") != DELIVERY_CONFIG_SCHEMA:
        raise DeliveryConfigError(
            f"delivery_config_schema_unsupported:{data.get('schema')}")
    cfg_dir = config_path.parent

    def _resolve_list(entry: Any, field: str) -> list[Any] | None:
        if entry is None:
            return None
        if isinstance(entry, Mapping) and entry.get("path"):
            file_path = Path(str(entry["path"]))
            if not file_path.is_absolute():
                file_path = cfg_dir / file_path
            if not file_path.is_file():
                raise DeliveryConfigError(
                    f"delivery_config_file_missing:{field}:{file_path}")
            entry = json.loads(file_path.read_text(encoding="utf-8"))
        if isinstance(entry, Mapping) and "entries" in entry:
            entry = [entry]
        if not isinstance(entry, list):
            raise DeliveryConfigError(f"delivery_config_field_not_list:{field}")
        return entry

    def _stage_input(entry: Any, field: str) -> tuple[Path | None,
                                                      dict[str, Any] | None]:
        if entry is None:
            return None, None
        if not isinstance(entry, Mapping):
            raise DeliveryConfigError(f"delivery_config_stage_bad:{field}")
        fixture = entry.get("fixture")
        recordings = entry.get("recordings")
        if bool(fixture) == bool(recordings):
            raise DeliveryConfigError(
                f"delivery_config_stage_needs_exactly_one:{field}")

        def resolve(value: str | Path) -> Path:
            file_path = Path(str(value))
            return file_path if file_path.is_absolute() else cfg_dir / file_path

        if fixture:
            fixture_path = resolve(fixture)
            if not fixture_path.is_file():
                raise DeliveryConfigError(
                    f"delivery_config_file_missing:{field}:{fixture_path}")
            return fixture_path, None
        recordings_path = resolve(recordings)
        if not recordings_path.is_file():
            raise DeliveryConfigError(
                f"delivery_config_file_missing:{field}:{recordings_path}")
        return None, load_recordings(recordings_path)

    def _int_map(mapping: Any) -> dict[int, int]:
        return {int(k): int(v) for k, v in dict(mapping or {}).items()}

    text_edit_fixture, text_edit_recordings = _stage_input(
        data.get("text_edit"), "text_edit")
    front_back_entry = data.get("front_back")
    if front_back_entry is not None and not isinstance(front_back_entry, Mapping):
        raise DeliveryConfigError("delivery_config_stage_bad:front_back")
    front_back_entry = dict(front_back_entry or {})
    front_back_mode = str(front_back_entry.get("mode") or "legacy")
    if front_back_mode not in ("legacy", "post_body"):
        raise DeliveryConfigError(f"delivery_config_front_back_mode_unsupported:{front_back_mode}")
    if front_back_mode == "post_body" and not (
        front_back_entry.get("fixture") or front_back_entry.get("recordings")
    ):
        front_back_fixture, front_back_recordings = None, None
    else:
        front_back_fixture, front_back_recordings = _stage_input(
            data.get("front_back"), "front_back")
    post_body_context: dict[str, Any] = {}
    if front_back_mode == "post_body":
        post_body_context = load_post_body_context(
            front_back_entry.get("post_body_context", data.get("post_body_context")),
            base_dir=cfg_dir,
        )
        # Supplementary material may be supplied directly without a context
        # file or an earlier PartPlan; the actual BODY remains the main input.
        direct_context = {
            field: front_back_entry.get(field, data.get(field))
            for field in ("material_records", "source_identity_map")
            if field in front_back_entry or field in data
        }
        if direct_context:
            post_body_context.update(load_post_body_context(direct_context, base_dir=cfg_dir))
    figure_assets = _resolve_list(
        data.get("figure_assets"), "figure_assets") or []
    # Each asset's own file path also resolves relative to the config file.
    resolved_assets: list[Any] = []
    for asset in figure_assets:
        if isinstance(asset, Mapping) and asset.get("path"):
            asset_path = Path(str(asset["path"]))
            if not asset_path.is_absolute():
                asset_path = cfg_dir / asset_path
            if not asset_path.is_file():
                raise DeliveryConfigError(
                    f"delivery_config_file_missing:figure_assets:{asset_path}")
            resolved_assets.append({**dict(asset), "path": str(asset_path)})
        else:
            resolved_assets.append(asset)
    return {
        "config_path": str(config_path),
        "language": str(data.get("language") or "zh"),
        "research_question": str(data.get("research_question") or post_body_context.get("research_question") or "").strip(),
        "chapter_roles": [dict(row)
                          for row in (data.get("chapter_roles") or [])
                          if isinstance(row, Mapping)],
        "text_edit_fixture": text_edit_fixture,
        "text_edit_recordings": text_edit_recordings,
        "front_back_fixture": front_back_fixture,
        "front_back_recordings": front_back_recordings,
        "front_back_mode": front_back_mode,
        "post_body_context": post_body_context,
        "identity_catalogs": _resolve_list(
            data.get("identity_catalogs"), "identity_catalogs") or [],
        "identity_catalogs_explicit": "identity_catalogs" in data,
        "figure_assets": resolved_assets,
        "table_moves": _int_map(data.get("table_moves")),
        "figure_moves": _int_map(data.get("figure_moves")),
        "compile_pdf": bool(data.get("compile_pdf", False)),
    }


def _stage_pending(stage: str, reason: str) -> dict[str, Any]:
    return {"stage": stage, "status": "pending", "reason": reason,
            "model_calls": 0, "external_requests": 0}


def _post_body_identity_context(
    context: Mapping[str, Any], catalogs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate exact citation ownership before generation, without routing.

    Bare handles follow stage 04's primary-catalog convention.  Namespaced
    handles remain distinct.  The serial runtime keeps this full inventory
    local and exposes only BODY/selected-material identities in its prompts.
    """
    if not catalogs and not context.get("source_identity_map") and not context.get("material_records"):
        return dict(context)
    from .delivery_citations import _iter_catalog_identities
    from .serial_manuscript_parts import (
        merge_source_identity_maps, normalize_source_identity_map,
    )
    indexed: dict[str, Any] = {}
    primary_namespace = str(catalogs[0].get("namespace") or "") if catalogs else ""
    for catalog in catalogs:
        namespace = str(catalog.get("namespace") or "")
        for row in _iter_catalog_identities(catalog):
            handles = row.get("handles") or [
                row.get("source_handle") or row.get("handle") or row.get("source_key") or ""]
            if isinstance(handles, str):
                handles = [handles]
            for handle in handles:
                handle = str(handle).strip()
                if not handle:
                    continue
                token = f"{namespace}::{handle}" if namespace else handle
                indexed = merge_source_identity_maps(
                    indexed, {token: {**dict(row), "source_handle": token}})
    explicit_bare = {token for token in indexed if "::" not in token}
    # This mirrors stage 04: an explicit bare declaration wins; otherwise
    # only the primary namespace supplies aliases for bare BODY citations.
    if primary_namespace:
        prefix = primary_namespace + "::"
        for token, identity in list(indexed.items()):
            if token.startswith(prefix):
                bare = token[len(prefix):]
                if bare not in indexed:
                    indexed[bare] = {**identity, "source_handle": bare}
    supplied = normalize_source_identity_map(
        context.get("source_identity_map", {}),
        material_records=context.get("material_records", []),
    )
    # The same paper may be addressed with a primary namespace or its bare
    # alias; validate both spellings against supplied declarations.
    if primary_namespace:
        for token, identity in list(supplied.items()):
            alias = token[len(primary_namespace) + 2:] if token.startswith(primary_namespace + "::") \
                else f"{primary_namespace}::{token}" if "::" not in token else None
            bare = token.split("::", 1)[-1]
            if alias and bare not in explicit_bare:
                for source in (indexed, supplied):
                    if alias in source:
                        merge_source_identity_maps({alias: source[alias]},
                                                   {alias: {**identity, "source_handle": alias}})
    merged = merge_source_identity_maps(indexed, supplied)
    # Catch catalog-resolution ambiguity before any model/provider invocation.
    _post_body_delivery_catalogs(catalogs, supplied)
    return {**dict(context), "source_identity_map": merged}


def _post_body_delivery_catalogs(
    catalogs: Sequence[Mapping[str, Any]], identities: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Add only the runtime's validated selected identities to stage 04."""
    from .delivery_citations import _iter_catalog_identities
    from .manuscript_front_back import FrontBackError
    result: list[dict[str, Any]] = []
    for catalog in catalogs:
        entries = []
        for row in _iter_catalog_identities(catalog):
            handles = row.get("handles") or [
                row.get("source_handle") or row.get("handle") or row.get("source_key") or ""]
            if isinstance(handles, str):
                handles = [handles]
            entries.append({**dict(row), "handles": [str(h) for h in handles if str(h).strip()]})
        result.append({"namespace": str(catalog.get("namespace") or ""), "entries": entries})
    primary_namespace = str(catalogs[0].get("namespace") or "") if catalogs else ""
    explicit_bare = {
        handle for catalog in result if not catalog["namespace"]
        for row in catalog["entries"] for handle in row["handles"]
    }
    additions: dict[str, list[dict[str, Any]]] = {}
    for token, identity in identities.items():
        namespace, handle = token.split("::", 1) if "::" in token \
            else ("" if token in explicit_bare else primary_namespace, token)
        additions.setdefault(namespace, []).append(
            {**dict(identity), "source_handle": handle, "handles": [handle]})
    for namespace, entries in additions.items():
        result.append({"namespace": namespace, "entries": entries})
    handle_only_namespaces: dict[str, set[str]] = {}
    for catalog in result:
        for row in catalog["entries"]:
            if row.get("doi") or row.get("paper_id") or not row.get("handles"):
                continue
            canonical_handle = row["handles"][0]
            namespaces = handle_only_namespaces.setdefault(canonical_handle, set())
            namespaces.add(catalog["namespace"])
            if len(namespaces) > 1:
                raise FrontBackError(
                    f"post_body_ambiguous_identity:{canonical_handle}:namespace_collision_requires_doi_or_paper_id")
    return result


def run_downstream_delivery(
    *,
    config: Mapping[str, Any],
    assembly_report: Mapping[str, Any],
    out_dir: str | Path,
    selected_body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The ONE downstream coordinator: 02→03→04→05 over the assembled draft.

    Each stage consumes the previous stage's ACTUAL returned artifact (the
    report field), never a guessed path.  02 edits the assembled HANDLE draft
    so 04 still sees source identity; 04's numbered reader draft is the only
    publication input.  A stage without its config input reports ``pending``
    and the chain stops there — it never falls back to a stale artifact and
    never degrades to assembly-only.
    """

    out_dir = Path(out_dir).resolve()
    stages: dict[str, Any] = {}
    halt: list[str] = []

    # History/plan readable drafts can be restricted/pending exports. Their
    # assembler's "complete" only means all selected units were loaded; its
    # separate unresolved/problem fields must also permit downstream work.
    # Check before looking for a file, so a stale prior draft cannot bypass a
    # failed arrangement or missing recording in the current invocation. BODY
    # has its own complete-generation/stage gate and retains science diagnostics.
    assembly = assembly_report.get("assembly")
    # Identity-only gaps have an existing downstream resolver using the
    # explicitly supplied catalogs.  Do not turn that normal path into a
    # task-completion failure; its later citation status remains authoritative.
    identity_only = (
        isinstance(assembly, Mapping)
        and bool(assembly.get("unknown_citations"))
        and not assembly.get("pending_problems")
        and not assembly.get("unknown_table_handles")
    )
    body_state = body_delivery_state(assembly_report, selected_body) if selected_body is not None else None
    if body_state is not None:
        assembly_blocked = not body_state["ready"]
    else:
        assembly_blocked = (not isinstance(assembly, Mapping)
            or assembly.get("status") != "complete"
            or (assembly.get("problems_resolved") is not True and not identity_only)
            or assembly.get("pending_problems")
            or assembly.get("missing_units")
            or assembly.get("errors")
            or assembly_report.get("pending")
            or assembly_report.get("missing_units")
            or assembly_report.get("restricted_import"))
    if assembly_blocked:
        report = _downstream_report(config, {}, ["assembly_pending"])
        report["assembly_gate"] = {
            "status": "pending",
            "assembly_status": assembly.get("status") if isinstance(assembly, Mapping) else None,
            "problems_resolved": assembly.get("problems_resolved") if isinstance(assembly, Mapping) else None,
            "reason": "body_delivery_incomplete" if body_state is not None else "assembly_incomplete_or_unresolved",
        }
        if body_state is not None:
            report["assembly_gate"]["blocking_reasons"] = body_state["blocking_reasons"]
        return report

    # The assembler writes BOTH drafts; downstream starts from the HANDLE
    # one.  history reports output_root=<assembled dir>, plan reports the
    # delivery root with the assembly one level below — resolve from the
    # report's own field instead of assuming either shape.
    output_root = Path(str(assembly_report.get("output_root") or ""))
    handle_draft = Path(selected_body["handles_draft"]) if selected_body is not None else next(
        (candidate for candidate in (
            output_root / "REVIEW_DRAFT_HANDLES.md",
            output_root / "assembled" / "REVIEW_DRAFT_HANDLES.md")
         if candidate.is_file()), None)
    if handle_draft is None:
        return {"delivery_mode": "full_downstream",
                "config": str(config.get("config_path") or ""),
                "stages": {},
                "halt_reasons": [f"handle_draft_missing:{output_root}"],
                "downstream_status": "pending"}

    # The selected BODY already owns a validated references catalog. Keep an
    # explicit configured inventory authoritative; otherwise carry this run's
    # actual catalog to both independent parts and the final citation pass.
    if selected_body is not None and not config.get("identity_catalogs_explicit", "identity_catalogs" in config):
        catalog = json.loads(Path(selected_body["references_path"]).read_text(encoding="utf-8"))
        config = {**config, "identity_catalogs": [catalog]}

    # ---- 02: full-text editing over the handle draft -----------------------
    if selected_body is not None:
        # BODY already made its one optional edit through the owned factory.
        # Configured front/back work consumes that exact selected body.
        stages["02_text_edit"] = {"stage": "text_edit", "status": "body_selected",
            "edited_draft": selected_body["handles_draft"], "model_calls": 0,
            "external_requests": 0, "source": selected_body["source"]}
    elif config.get("text_edit_fixture") is None and \
            config.get("text_edit_recordings") is None:
        stages["02_text_edit"] = _stage_pending(
            "text_edit", "no_fixture_or_recordings_in_config")
    else:
        from .article_text_editor import TextEditError, run_text_edit_stage
        try:
            stages["02_text_edit"] = run_text_edit_stage(
                draft_path=handle_draft,
                chapter_roles=config.get("chapter_roles") or [],
                out_dir=out_dir / "02_text_edit",
                proposals_fixture_path=config.get("text_edit_fixture"),
                recordings=config.get("text_edit_recordings"),
                language=str(config.get("language") or "zh"))
        except TextEditError as exc:
            stages["02_text_edit"] = {"stage": "text_edit", "status": "failed",
                                      "error": str(exc),
                                      "model_calls": 0, "external_requests": 0}
    r2 = stages["02_text_edit"]
    edited = Path(str(r2.get("edited_draft") or ""))
    if r2.get("status") == "pending" or not str(r2.get("edited_draft") or "") \
            or not edited.is_file():
        halt.append("02_text_edit")
        return _downstream_report(config, stages, halt)

    # ---- 03: front/back parts over the edited body -------------------------
    if config.get("front_back_fixture") is None and \
            config.get("front_back_recordings") is None:
        stages["03_front_back"] = _stage_pending(
            "front_back", "no_fixture_or_recordings_in_config")
    else:
        from .manuscript_front_back import FrontBackError, run_front_back_stage
        try:
            runner = run_front_back_stage
            extra: dict[str, Any] = {}
            if config.get("front_back_mode") == "post_body":
                from .serial_manuscript_parts import run_serial_parts
                runner = run_serial_parts
                extra["context"] = _post_body_identity_context(
                    config.get("post_body_context") or {}, config.get("identity_catalogs") or [])
            stages["03_front_back"] = runner(
                draft_path=edited,
                research_question=str(config.get("research_question") or ""),
                chapter_roles=config.get("chapter_roles") or [],
                out_dir=out_dir / "03_front_back",
                parts_fixture_path=config.get("front_back_fixture"),
                recordings=config.get("front_back_recordings"),
                language=str(config.get("language") or "zh"),
                **extra)
        except FrontBackError as exc:
            stages["03_front_back"] = {"stage": "front_back", "status": "failed",
                                       "error": str(exc),
                                       "final_manuscript": None,
                                       "model_calls": 0, "external_requests": 0}
    r3 = stages["03_front_back"]
    final_md = Path(str(r3.get("final_manuscript") or ""))
    if ((selected_body is not None or config.get("front_back_mode") == "post_body") and r3.get("status") != "generated") \
            or r3.get("status") in ("pending", "no_parts_generated", "failed") \
            or not str(r3.get("final_manuscript") or "") or not final_md.is_file():
        halt.append("03_front_back")
        return _downstream_report(config, stages, halt)

    # ---- 04: figures + citations over the FINAL manuscript -----------------
    from .delivery_citations import run_figures_citations_stage
    identity_catalogs = config.get("identity_catalogs") or []
    if config.get("front_back_mode") == "post_body":
        try:
            identity_catalogs = _post_body_delivery_catalogs(
                identity_catalogs, r3.get("source_identity_map") or {})
        except FrontBackError as exc:
            stages["04_figures_citations"] = {
                "stage": "figures_citations", "status": "failed",
                "error": str(exc), "model_calls": 0, "external_requests": 0,
            }
            return _downstream_report(config, stages, ["04_figures_citations"])
    numbering_input = final_md
    projection = None
    if selected_body is not None and config.get("front_back_mode") == "post_body":
        numbering_input, projection = _post_body_numbering_input(final_md, out_dir, config.get("chapter_roles") or [])
    stages["04_figures_citations"] = run_figures_citations_stage(
        final_draft_path=numbering_input,
        identity_catalogs=identity_catalogs,
        out_dir=out_dir / "04_figures_citations",
        figure_assets=config.get("figure_assets") or [],
        table_moves=config.get("table_moves") or None,
        figure_moves=config.get("figure_moves") or None)
    r4 = stages["04_figures_citations"]
    if projection is not None:
        r4["input_projection"] = projection
        _write_json(out_dir / "04_figures_citations" / "STAGE_REPORT.json", r4)
    reader_draft = Path(str(r4.get("reader_draft") or ""))
    if ((selected_body is not None or config.get("front_back_mode") == "post_body") and r4.get("status") != "complete") \
            or not str(r4.get("reader_draft") or "") or not reader_draft.is_file():
        halt.append("04_figures_citations")
        return _downstream_report(config, stages, halt)

    # ---- 05: publication consuming the 04 artifacts only -------------------
    stages["05_publication"] = run_publication_delivery(
        reader_draft_path=reader_draft,
        references_path=Path(str(r4.get("references_path") or "")),
        figure_map_path=Path(str(r4.get("mapping_path") or "")),
        assets_dir=Path(str(r4.get("assets_dir") or "")),
        out_dir=out_dir / "05_publication",
        language=str(config.get("language") or "zh"),
        compile_pdf=bool(config.get("compile_pdf")))
    r5 = stages["05_publication"]

    return _downstream_report(config, stages, halt)


def _downstream_report(config: Mapping[str, Any], stages: Mapping[str, Any],
                       halt: Sequence[str]) -> dict[str, Any]:
    """Overall downstream status: complete only when every stage really ran
    and none of them is pending or failed.  A carried 04 pending (unknown
    identity handles) keeps the whole delivery pending — never overwritten to
    complete by a later stage."""
    r4 = stages.get("04_figures_citations") or {}
    r5 = stages.get("05_publication") or {}
    if halt:
        overall = "pending"
    elif r4.get("status") != "complete":
        overall = "pending"
    elif r5.get("status") != "complete":
        overall = str(r5.get("status") or "pending")
    else:
        overall = "complete"
    return {"delivery_mode": "full_downstream",
            "config": str(config.get("config_path") or ""),
            "stages": dict(stages),
            "halt_reasons": list(halt),
            "downstream_status": overall}


def _post_body_numbering_input(source: Path, out_dir: Path, chapter_roles: Sequence[Mapping[str, Any]]) -> tuple[Path, dict[str, Any] | None]:
    """Remove only old unowned bibliography; preserve owned parts verbatim.

    The citation renderer otherwise treats a following owned-part start marker
    as old bibliography content before its next heading. Keep the actual 03
    manuscript and record this local numbering projection separately.
    """
    from .serial_parts_application import _structure
    from .article_text_editor import _numbering_projection
    text = source.read_text(encoding="utf-8")
    spans, _, _ = _structure(text, chapter_roles)
    chunks, cursor = [], 0
    for span in sorted(spans, key=lambda row: row.start):
        chunks.extend((_numbering_projection(text[cursor:span.start]), text[span.start:span.end]))
        cursor = span.end
    chunks.append(_numbering_projection(text[cursor:]))
    projected = "".join(chunks)
    if projected == text:
        return source, None
    target = out_dir / "04_NUMBERING_INPUT_HANDLES.md"
    target.write_text(projected, encoding="utf-8", newline="\n")
    return target, {"source_draft": str(source), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "numbering_input": str(target), "numbering_input_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "reason": "exclude_old_unowned_bibliography_preserve_owned_parts"}


def _body_delivery_artifacts(report: Mapping[str, Any], out_dir: Path) -> dict[str, Any]:
    """Select the actual edited BODY and reuse the single citation renderer."""
    if not report.get("assembly"):
        return {"status": "not_generated", "source": "none", "handles_draft": None,
                "reader_draft": None, "references_path": None}
    article = report.get("article_edit") or {}
    selected = out_dir / "assembled" / "REVIEW_DRAFT_HANDLES.md"
    source = "assembly"
    numbering = None
    if article.get("status") in {"edited", "no_change"} and article.get("edited_draft"):
        selected = Path(article["edited_draft"])
        source = "article_edit"
        numbering = article.get("numbering")
    if not selected.is_file():
        raise ValueError("body_selected_draft_missing:" + str(selected))
    if not numbering:
        from .article_text_editor import _numbering_projection
        from .delivery_citations import run_figures_citations_stage
        catalog = json.loads((out_dir / "assembled" / "REFERENCES.json").read_text(encoding="utf-8"))
        projection = out_dir / "BODY_NUMBERING_INPUT_HANDLES.md"
        projection.write_text(_numbering_projection(selected.read_text(encoding="utf-8")),
                              encoding="utf-8", newline="\n")
        numbering = run_figures_citations_stage(final_draft_path=projection,
            identity_catalogs=[catalog], out_dir=out_dir / "body_numbered")
    return {"status": numbering["status"], "source": source,
        "handles_draft": str(selected.resolve()), "reader_draft": numbering["reader_draft"],
        "references_path": numbering["references_path"], "numbering": numbering}


def body_delivery_state(report: Mapping[str, Any], selected_body: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Separate finished BODY diagnostics from missing generation/stages."""
    selected = selected_body if selected_body is not None else report.get("selected_body") or {}
    assembly = report.get("assembly") or {}
    settings = report.get("effective_settings") or {}
    units = report.get("units") or []
    blockers = []
    full_count = report.get("full_unit_count") or 0
    if (not full_count or not report.get("generation_complete") or report.get("missing_units")
            or len(units) != full_count or report.get("complete_units") != full_count):
        blockers.append("body_generation_incomplete")
    if (assembly.get("status") != "complete" or assembly.get("missing_units") or assembly.get("errors")
            or assembly.get("loaded_units") != report.get("full_unit_count")
            or report.get("pending") or report.get("restricted_import")):
        blockers.append("body_assembly_incomplete")
    if selected.get("status") != "complete":
        blockers.append("body_numbering_incomplete")
    for field in ("handles_draft", "reader_draft", "references_path"):
        path = Path(str(selected.get(field) or ""))
        if not path.is_file() or path.stat().st_size == 0:
            blockers.append("body_artifact_missing:" + field)
    if settings.get("quality_control"):
        from .legacy_unit_route import _quality_body_ready_for_article
        for row in units:
            quality = row.get("quality") or {}
            if (quality.get("status") not in {"assessed_pending_human_review", "assessed_with_pending"}
                    or not quality.get("assessments") or not _quality_body_ready_for_article(row)
                    or any(attempt.get("state") not in {"returned", "cache_hit"}
                           for attempt in quality.get("stage_attempts", []))):
                blockers.append("body_quality_incomplete:" + str(row.get("chapter_id")) + ":" + str(row.get("unit_id")))
    if settings.get("article_edit"):
        article = report.get("article_edit") or {}
        if (article.get("status") not in {"edited", "no_change"} or selected.get("source") != "article_edit"
                or Path(str(article.get("edited_draft") or "")).resolve()
                   != Path(str(selected.get("handles_draft") or "")).resolve()):
            blockers.append("body_article_edit_incomplete")
    diagnostics = bool(assembly.get("pending_problems") or assembly.get("problems_resolved") is False
                       or any(row.get("pending_problems") for row in units))
    ready = not blockers
    return {"status": "complete_with_diagnostics" if ready and diagnostics else "complete" if ready else
            "preview" if report.get("status") == "preview" else "incomplete",
            "ready": ready, "blocking_reasons": blockers, "diagnostics_pending": diagnostics,
            "scientific_acceptance": False}


def body_delivery_exit_code(report: Mapping[str, Any]) -> int:
    """Both BODY CLIs share artifact/stage success, independent of science."""
    state = report.get("body_delivery") or body_delivery_state(report)
    if state["status"] == "preview":
        return 0
    if not state["ready"]:
        return 2
    if report.get("delivery_mode") == "full_downstream" and report.get("downstream_status") != "complete":
        return 2
    return 0


def run_review_delivery(
    *,
    start: str,
    out_dir: str | Path,
    language: str = "zh",
    manifest_path: str | Path | None = None,
    batch_root: str | Path | None = None,
    packet_path: str | Path | None = None,
    recordings_path: str | Path | None = None,
    config_path: str | Path | None = None,
    body_input: Mapping[str, Any] | None = None,
    body_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Official delivery entry.

    The body branch selects its actual optional edited BODY and numbered reader
    draft. Other branches without ``config_path`` stop after assembly. With a
    config the selected result enters the
    unified downstream (02 edit → 03 front/back → 04 figures/citations → 05
    publication); missing inputs surface as explicit pending stages.
    """
    out_dir = Path(out_dir)
    if start == "history":
        if not manifest_path or not batch_root:
            raise ValueError("history_start_requires_manifest_and_batch_root")
        report = run_history_delivery(manifest_path=manifest_path, batch_root=batch_root,
                                      out_dir=out_dir, language=language)
    elif start == "plan":
        if not packet_path or not recordings_path:
            raise ValueError("plan_start_requires_packet_and_recordings")
        report = run_plan_delivery(packet_path=packet_path, recordings_path=recordings_path,
                                   out_dir=out_dir, language=language)
    elif start == "body":
        if not isinstance(body_input, Mapping):
            raise ValueError("body_start_requires_full_body_input")
        from .legacy_unit_route import run_legacy_units
        options = {**BODY_DELIVERY_DEFAULTS, **dict(body_options or {})}
        report = run_legacy_units(body_input, output_dir=out_dir, **options)
        report = {**report, "start": "body", "output_root": str(out_dir.resolve())}
    else:
        raise ValueError(f"unknown_delivery_start:{start}")

    if start == "body":
        report["selected_body"] = _body_delivery_artifacts(report, out_dir)
        report["body_delivery"] = body_delivery_state(report)
    if config_path is None:
        if start == "body":
            report = {**report, "delivery_mode": "body_only"}
            _write_json(out_dir / "DELIVERY_REPORT.json", report)
            return report
        report = {**report, "delivery_mode": "assembly_only",
                  "note": "no delivery config: assembly only, downstream not run"}
        _write_json(out_dir / "DELIVERY_REPORT.json", report)
        return report
    config = load_delivery_config(config_path)
    downstream = run_downstream_delivery(config=config, assembly_report=report,
                                         out_dir=out_dir,
                                         **({"selected_body": report["selected_body"]} if start == "body" else {}))
    merged = {**report, **downstream}
    if start == "body":
        merged["model_calls"] = report["model_calls"] + downstream.get("model_calls", 0)
    _write_json(out_dir / "DELIVERY_REPORT.json", merged)
    return merged


# ---------------------------------------------------------------------------
# publication (05): offline Markdown + TeX/PDF from the final draft
# ---------------------------------------------------------------------------


def run_publication_delivery(
    *,
    reader_draft_path: str | Path,
    references_path: str | Path,
    figure_map_path: str | Path | None = None,
    assets_dir: str | Path | None = None,
    out_dir: str | Path,
    language: str = "zh",
    compile_pdf: bool = False,
) -> dict[str, Any]:
    """Publication (05) consuming the 04 artifacts — and nothing else.

    The 04 reader draft is ALREADY numbered and carries its single reference
    section; this stage copies it verbatim into ``MANUSCRIPT_PUBLISHED.md``,
    extracts the real title/abstract/keywords for the TeX metadata, copies
    the figure assets so the Markdown and TeX relative paths both resolve,
    and (only when ``compile_pdf`` is set) builds TeX/PDF via the existing
    publisher with Crossref/Semantic Scholar disabled.  It does NOT re-attach
    figures, re-number figure tokens, rewrite cross-references, or rebuild
    the citation map — those belong to stage 04 alone.  A malformed 04 input
    fails loudly instead of publishing.
    """

    from optomind_research.runtime.latex_publication_renderer import (
        build_latex_publication,
    )

    reader_path = Path(reader_draft_path).resolve()
    references_path = Path(references_path).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    reader_text = reader_path.read_text(encoding="utf-8")
    refs_doc = json.loads(references_path.read_text(encoding="utf-8"))
    refs = refs_doc.get("references") or []

    # --- verify the 04 output instead of re-deriving it ---------------------
    checks = _verify_reader_draft(reader_text, expected_refs=len(refs))
    report: dict[str, Any] = {
        "stage": "publication_delivery",
        "reader_draft": str(reader_path),
        "references_source": str(references_path),
        "verification": checks,
        "unknown_citation_tokens_carried": refs_doc.get("unknown_handles") or [],
        "model_calls": 0,
        "external_requests": 0,
    }
    if not checks["all_pass"]:
        report["status"] = "failed"
        report["failure_reasons"] = checks["failures"]
        _write_json(out_dir / "DELIVERY_REPORT.json", report)
        return report

    # --- Markdown: the 04 body is the published body ------------------------
    published_path = out_dir / "MANUSCRIPT_PUBLISHED.md"
    published_path.write_text(
        reader_text.rstrip() + "\n", encoding="utf-8", newline="\n")
    metadata = _extract_front_matter(reader_text)
    _write_json(out_dir / "PUBLICATION_METADATA.json", metadata)
    report["publication_metadata"] = metadata
    report["published_markdown"] = str(published_path)

    # --- assets: resolvable next to the Markdown AND inside tex/ ------------
    copied_assets: list[str] = []
    source_assets = Path(assets_dir).resolve() if assets_dir else None
    if source_assets is not None and source_assets.is_dir():
        for target in (out_dir / "assets", out_dir / "tex" / "assets"):
            target.mkdir(parents=True, exist_ok=True)
            for asset in source_assets.iterdir():
                if asset.is_file():
                    shutil.copyfile(asset, target / asset.name)
        copied_assets = sorted(a.name for a in (out_dir / "assets").iterdir())
    report["assets_copied"] = copied_assets

    # --- TeX/PDF (optional, offline) ----------------------------------------
    tex_result: dict[str, Any] = {"compile_requested": bool(compile_pdf)}
    if compile_pdf:
        pkg_path = out_dir / "CONTENT_PACKAGE.json"
        _write_json(pkg_path, {
            "source_run_dir": str(out_dir.parent),
            "final_review_path": str(published_path),
            "publication_metadata_path": str(out_dir / "PUBLICATION_METADATA.json"),
        })
        try:
            pub_result = build_latex_publication(
                content_package_path=pkg_path,
                output_dir=out_dir / "tex",
                source_markdown_path=published_path,
                language=language,
                enrich_crossref=False,
                enrich_s2=False,
                compile_pdf=True,
                pdf_strict=False,
            )
            tex_result.update({k: pub_result.get(k)
                               for k in ("pdf_path", "tex_path",
                                         "pdf_skipped_reason", "status")
                               if k in pub_result})
        except Exception as exc:  # a broken toolchain is a RESTRICTED result
            tex_result = {"compile_requested": True, "error": str(exc)}
        # The built PDF is whatever actually sits in the tex directory.
        pdf_candidate = out_dir / "tex" / "main.pdf"
        if not tex_result.get("pdf_path") and pdf_candidate.is_file():
            tex_result["pdf_path"] = str(pdf_candidate)
    report["tex_result"] = tex_result
    pdf_path = Path(str(tex_result.get("pdf_path") or ""))
    pdf_missing = bool(compile_pdf) and not (pdf_path.is_file())
    report["status"] = "restricted" if pdf_missing else "complete"
    if pdf_missing:
        report["failure_reasons"] = ["pdf_not_produced"]
    _write_json(out_dir / "DELIVERY_REPORT.json", report)
    return report


_REFS_HEADING_COUNT_RE = re.compile(r"^#{1,3}\s+参考文献", re.MULTILINE)
_NUMBERED_REF_RE = re.compile(r"^\[\d+\]", re.MULTILINE)
_ABSTRACT_HEADING_RE = re.compile(r"^#{2,3}\s+(?:摘要|Abstract)\s*$", re.MULTILINE)
_KEYWORDS_LINE_RE = re.compile(r"^\*\*关键词[：:]\*\*.*$", re.MULTILINE)


def _verify_reader_draft(reader_text: str, *, expected_refs: int) -> dict[str, Any]:
    """The 04 reader draft must already be publication-shaped: one reference
    section with the catalogued numbered entries, no figure placeholder left,
    a real title.  Failures are listed, never repaired silently here."""
    ref_sections = len(_REFS_HEADING_COUNT_RE.findall(reader_text))
    numbered = len(_NUMBERED_REF_RE.findall(reader_text))
    failures: list[str] = []
    if ref_sections != 1:
        failures.append(f"reference_sections_not_unique:{ref_sections}")
    if expected_refs and numbered != expected_refs:
        failures.append(f"numbered_refs_mismatch:{numbered}!={expected_refs}")
    if "@@FIG:" in reader_text:
        failures.append("figure_placeholder_token_left")
    if not re.search(r"^#\s+\S", reader_text, re.MULTILINE):
        failures.append("title_missing")
    return {"reference_sections_count": ref_sections,
            "numbered_reference_entries": numbered,
            "catalogued_references": expected_refs,
            "failures": failures,
            "all_pass": not failures}


def _extract_front_matter(text: str) -> dict[str, Any]:
    """Real title/abstract/keywords from the manuscript's own headings.

    The abstract is the text between the 摘要 heading and the next heading,
    WITHOUT the keyword line (keywords are their own metadata field).  A
    missing title stays empty and lets verification fail — never a default
    placeholder.
    """
    owned = "<!-- manuscript-part:abstract:start -->" in text
    if owned:
        from .serial_parts_application import extract_owned_parts
        parts = extract_owned_parts(text)
        # Read only declared front matter. BODY may itself contain a
        # substantive Abstract/摘要 heading, and marker comments are not prose.
        # Literal marker examples inside code do not establish ownership.
        owned = "abstract" in parts
        if owned:
            text = parts.get("title", "") + "\n\n" + parts["abstract"]
    keyword_line = re.compile(r"^\*\*(?:关键词|Keywords)[：:]\*\*.*$", re.I) \
        if owned else _KEYWORDS_LINE_RE
    title = ""
    abstract = ""
    keywords: list[str] = []
    lines = text.splitlines()
    in_abstract = False
    for line in lines:
        if line.startswith("# ") and not title:
            title = line.lstrip("# ").strip()
        if _ABSTRACT_HEADING_RE.match(line):
            in_abstract = True
            continue
        if in_abstract and line.startswith("#"):
            in_abstract = False
        if in_abstract and not keyword_line.match(line):
            abstract += line + "\n"
        match = re.search(r"\*\*(?:关键词|Keywords)[：:]\*\*\s*(.+)", line, re.I) \
            if owned else re.search(r"\*\*关键词[：:]\*\*\s*(.+)", line)
        if match:
            tokens = re.split(r"[；;]", match.group(1)) if owned else match.group(1).split("；")
            keywords = [k.strip() for k in tokens if k.strip()]
    return {"title": title, "abstract": abstract.strip(),
            "keywords": keywords}
