"""Full-text local editing for the delivery backend (work order 02).

One whole-manuscript coordination pass over the REAL handle draft: the model
(offline: replay or a labeled manual fixture) proposes a small number of
executable local edits; this module parses that proposal and applies it to
the draft deterministically.  The old ``staged_article_completion`` block
contract (target block + verbatim original text) is reused as the targeting
rule: an edit applies only when ``original_text`` occurs exactly once in the
current draft, so re-running an applied edit is a no-op and a stale target is
skipped instead of guessed at.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

PROMPT_PATH = Path("prompts/article_text_editor.md")
# Above this size the draft no longer fits one coordination pass and must be
# read chapter by chapter (each pass still carries the complete chapter text).
FULL_TEXT_SINGLE_PASS_CHAR_LIMIT = 400_000


class TextEditError(ValueError):
    pass


def load_editor_prompt(path: str | Path | None = None) -> str:
    target = Path(path) if path else Path(__file__).resolve().parents[3] / PROMPT_PATH
    if not target.is_file():
        raise TextEditError(f"article_text_editor_prompt_missing:{target}")
    return target.read_text(encoding="utf-8")


def build_edit_passes(
    *,
    draft_text: str,
    chapter_roles: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build the real coordination message(s) over the actual full text.

    The whole draft is included verbatim in one coordination pass.  Drafts
    over ``FULL_TEXT_SINGLE_PASS_CHAR_LIMIT`` are rejected explicitly:
    chapter-wise reading with cross-chapter coordination is NOT implemented,
    and this module must not claim support it does not have.
    """

    if len(draft_text) > FULL_TEXT_SINGLE_PASS_CHAR_LIMIT:
        raise TextEditError(
            f"draft_too_long_for_single_pass:{len(draft_text)}>"
            f"{FULL_TEXT_SINGLE_PASS_CHAR_LIMIT}:chapter_wise_reading_not_implemented")
    roles_block = json.dumps(
        [{"chapter_id": row.get("chapter_id"), "title": row.get("title"),
          "role": row.get("role")} for row in chapter_roles],
        ensure_ascii=False, indent=2)
    system = load_editor_prompt()
    user = (
        "【章节职责】\n" + roles_block
        + "\n\n【全文句柄稿】\n" + draft_text
        + "\n\n【本轮交付】只返回 {\"changes\": [...], \"unresolved_questions\": [...]}；"
          "没有值得修改之处返回空 changes。"
    )
    return [{"scope": "full", "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]}]


def parse_edit_proposals(response: Any) -> dict[str, Any]:
    """Parse and validate the coordination response.

    Returns ``{"changes": [...], "unresolved_questions": [...], "no_change": bool}``.
    An empty change list is a legitimate no_change, never an error.
    """

    if isinstance(response, Mapping):
        content = response.get("content")
    else:
        content = response
    if isinstance(content, str):
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise TextEditError(f"edit_proposals_not_json:{exc}") from exc
    elif isinstance(response, Mapping) and ("changes" in response or "unresolved_questions" in response):
        data = response
    else:
        raise TextEditError("edit_proposals_unreadable")
    if not isinstance(data, Mapping):
        raise TextEditError("edit_proposals_not_object")
    for name in ("changes", "unresolved_questions"):
        rows = data.get(name, [])
        if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
            raise TextEditError(f"edit_proposals_invalid_rows:{name}")
    changes = [dict(item) for item in data.get("changes", [])]
    unresolved = [dict(item) for item in data.get("unresolved_questions", [])]
    for index, change in enumerate(changes):
        missing = [field for field in ("operation", "original_text")
                   if not isinstance(change.get(field), str) or not change[field].strip()]
        if missing:
            raise TextEditError(f"edit_change_missing_fields:{index}:{','.join(missing)}")
        if change["operation"] not in {"replace", "remove"}:
            raise TextEditError(f"edit_operation_unknown:{index}:{change['operation']}")
        if change["operation"] == "replace" and (not isinstance(change.get("replacement_text"), str) or not change["replacement_text"].strip()):
            raise TextEditError(f"edit_replace_without_replacement:{index}")
    return {"changes": changes, "unresolved_questions": unresolved,
            "no_change": not changes}


def apply_text_edits(
    draft_text: str,
    changes: Sequence[Mapping[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply parsed edits deterministically; report every skipped target.

    All targets are located on one immutable original snapshot. Overlapping
    targets are reported and additions cannot become later targets. Missing
    targets are no-ops, without claiming an unrelated replacement proves an
    earlier application. Append-style edits verify their addition at the target.
    """

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    intervals: list[tuple[int, int, str, Mapping[str, Any]]] = []
    for change in changes:
        original = str(change.get("original_text") or "")
        operation = str(change.get("operation") or "")
        replacement = str(change.get("replacement_text") or "") if operation == "replace" else ""

        def skip(reason: str) -> None:
            skipped.append({"operation": operation, "original_text": original[:120],
                "reason": reason, "target_block_id": str(change.get("target_block_id") or "")})

        count = draft_text.count(original) if original else 0
        if count != 1:
            # An addition elsewhere cannot prove this missing target was edited.
            # Missing targets remain no-ops, so reruns are still idempotent.
            skip(f"target_occurs_{count}_times")
            continue
        start = draft_text.index(original)
        end = start + len(original)
        if operation == "replace" and replacement.startswith(original):
            addition = replacement[len(original):]
            if addition.strip() and draft_text[end:].startswith(addition):
                skip("already_applied")
                continue
        if any(start < previous_end and previous_start < end
               for previous_start, previous_end, _, _ in intervals):
            skip("overlapping_target")
            continue
        intervals.append((start, end, replacement, change))
    text = draft_text
    for start, end, replacement, change in sorted(intervals, reverse=True, key=lambda row: row[0]):
        text = text[:start] + replacement + text[end:]
        applied.append({"operation": change["operation"],
            "original_text": change["original_text"][:120], "replacement_text": replacement[:120],
            "reason": str(change.get("reason") or ""),
            "target_block_id": str(change.get("target_block_id") or "")})
    return text, applied, skipped


def run_text_edit_stage(
    *,
    draft_path: str | Path,
    chapter_roles: Sequence[Mapping[str, Any]],
    out_dir: str | Path,
    proposals_fixture_path: str | Path | None = None,
    recordings: Mapping[str, Mapping[str, Any]] | None = None,
    language: str = "zh",
    client_factory=None, run: bool = False, token_counter=None,
    profile: Mapping[str, Any] | None = None,
    identity_catalogs: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """One coordination pass over the real draft, then deterministic apply.

    The proposal comes either from a labeled manual fixture file
    (``proposals_fixture_path``) or from a recorded coordination response
    (``recordings`` keyed ``text_edit:{scope}``).  Both routes go through the
    same real message construction, parser and applier.  The source draft is
    preserved; the edited result is written next to it as
    ``*_EDITED.md`` plus ``TEXT_EDIT_REPORT.json``.
    """

    draft_path = Path(draft_path).resolve()
    out_dir = Path(out_dir).resolve()
    draft_text = draft_path.read_text(encoding="utf-8")
    if client_factory is not None or run or profile is not None:
        if proposals_fixture_path or recordings is not None:
            raise TextEditError("edit_live_and_replay_inputs_conflict")
        return _run_live_edit(draft_path=draft_path, draft_text=draft_text,
            chapter_roles=chapter_roles, out_dir=out_dir, client_factory=client_factory,
            run=run, token_counter=token_counter, profile=profile,
            identity_catalogs=identity_catalogs)
    passes = build_edit_passes(draft_text=draft_text, chapter_roles=chapter_roles)
    messages_dir = out_dir / "messages"
    messages_dir.mkdir(parents=True, exist_ok=True)
    for pass_info in passes:
        scope = pass_info["scope"]
        (messages_dir / f"text_edit_{scope}_messages.json").write_text(
            json.dumps(pass_info["messages"], ensure_ascii=False, indent=2),
            encoding="utf-8")

    changes: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    proposals_source = ""
    if proposals_fixture_path:
        fixture = json.loads(Path(proposals_fixture_path).read_text(encoding="utf-8"))
        parsed = parse_edit_proposals({**fixture, "fixture": True})
        proposals_source = "labeled_manual_fixture:" + str(proposals_fixture_path)
        changes, unresolved = parsed["changes"], parsed["unresolved_questions"]
    else:
        from .review_delivery import ReplayClient, MissingRecording
        replay = ReplayClient(recordings or {})
        for pass_info in passes:
            key = f"text_edit:{pass_info['scope']}"
            replay.next(key, pass_info["messages"])
            try:
                response = replay(pass_info["messages"])
            except MissingRecording:
                pending = {"step": key, "status": "pending_missing_recording"}
                report = {"stage": "text_edit", "status": "pending",
                          "pending": [pending], "proposals_source": "recording",
                          "passes": [p["scope"] for p in passes],
                          "model_calls": 0, "external_requests": 0}
                _write_json(out_dir / "TEXT_EDIT_REPORT.json", report)
                return report
            parsed = parse_edit_proposals(response)
            changes.extend(parsed["changes"])
            unresolved.extend(parsed["unresolved_questions"])
        proposals_source = "recording"

    edited_text, applied, skipped = apply_text_edits(draft_text, changes)
    # The edited copy is a NEW artifact: it always goes to this stage's own
    # output directory, never next to a (historical) source draft.
    edited_path = out_dir / (draft_path.stem + "_EDITED.md")
    edited_path.write_text(edited_text, encoding="utf-8", newline="\n")
    report = {
        "stage": "text_edit",
        "status": "no_change" if not applied else "edited",
        "proposals_source": proposals_source,
        "changes_proposed": len(changes),
        "applied": applied,
        "skipped": skipped,
        "unresolved_questions": unresolved,
        "source_draft": str(draft_path),
        "edited_draft": str(edited_path),
        "passes": [p["scope"] for p in passes],
        "draft_chars_before": len(draft_text),
        "draft_chars_after": len(edited_text),
        "model_calls": 0,
        "external_requests": 0,
    }
    _write_json(out_dir / "TEXT_EDIT_REPORT.json", report)
    return report


def _run_live_edit(*, draft_path, draft_text, chapter_roles, out_dir,
                   client_factory, run, token_counter, profile, identity_catalogs):
    # Imports stay local: unit_realization imports this module's parser/applier.
    from .legacy_unit_route import _hash, _read, _write, _profile
    from .unit_realization import _cached_call
    from .writer_candidates import _output_lock
    effective = dict(profile or _profile("qwen3.5-plus", 24576, 16384))
    expected = _profile("qwen3.5-plus", 24576, 16384)
    if effective != expected:
        raise TextEditError("edit_requires_bounded_plus_profile")
    passes = build_edit_passes(draft_text=draft_text, chapter_roles=chapter_roles)
    messages = passes[0]["messages"]
    source_bytes = draft_path.read_bytes()
    identity = {"schema_version": "optomind.article_edit.live.v1",
        "draft_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "messages_sha256": _hash(messages), "profile": effective,
        "identity_catalogs_sha256": _hash(identity_catalogs or [])}
    with _output_lock(out_dir):
        identity_path = out_dir / "EDIT_IDENTITY.json"
        if identity_path.exists() and _read(identity_path) != identity:
            raise TextEditError("edit_input_identity_changed:use_new_output_directory")
        _write(identity_path, identity)
        original_path = out_dir / "ORIGINAL_HANDLES.md"
        if original_path.exists() and original_path.read_bytes() != source_bytes:
            raise TextEditError("edit_original_snapshot_integrity_failure")
        if not original_path.exists():
            original_path.write_bytes(source_bytes)
        report_path = out_dir / "TEXT_EDIT_REPORT.json"
        seal_path = out_dir / "EDIT_RESULT_SEAL.json"
        if report_path.exists() and seal_path.exists():
            saved, seal = _read(report_path), _read(seal_path)
            if _hash(saved) != seal.get("report_sha256"):
                raise TextEditError("edit_cached_report_integrity_failure")
            for name, digest in seal.get("files", {}).items():
                if hashlib.sha256((out_dir / name).read_bytes()).hexdigest() != digest:
                    raise TextEditError("edit_cached_artifact_integrity_failure")
            saved.update(model_calls=0, cache_hit=True)
            return saved
        step = out_dir / "full"
        if (step / "RAW_RESPONSE.json").exists() and not (step / "REQUEST.json").exists():
            raise TextEditError("edit_raw_without_request_identity")
        raw, calls, state = _cached_call(step, messages, effective, client_factory, run, token_counter)
        report = {"stage": "text_edit", "status": "pending", "proposals_source": "live_saved_response",
            "source_draft": str(draft_path), "original_snapshot": str(original_path),
            "output_dir": str(out_dir), "profile": effective, "model_calls": calls,
            "external_requests": calls, "cache_hit": state == "cache_hit", "call_state": state,
            "raw_response": str(step / "RAW_RESPONSE.json"), "passes": ["full"],
            "pending_problems": [], "scientific_acceptance": False}
        if raw is None:
            report["pending_problems"].append({"code": "article_edit_" + state})
            _write(report_path, report)
            return report
        if raw.get("fixture") is True:
            report["proposals_source"] = "labeled_manual_fixture_saved_response"
        elif raw.get("execution_mode") == "injected":
            report["proposals_source"] = "injected_saved_response"
        returned_model = raw.get("returned_model") or raw.get("model")
        if (not raw.get("complete") or raw.get("finish_reason") != "stop"
                or (returned_model and returned_model != effective["model"])):
            report["pending_problems"].append({"code": "article_edit_incomplete_or_model_mismatch"})
            _write(report_path, report)
            return report
        try:
            parsed = parse_edit_proposals(raw)
            edited, applied, skipped = apply_text_edits(draft_text, parsed["changes"])
        except TextEditError as exc:
            report["pending_problems"].append({"code": "article_edit_invalid_proposals", "error": str(exc)})
            _write(report_path, report)
            return report
        _write(out_dir / "EDIT_PROPOSALS.json", parsed)
        candidate = out_dir / "CANDIDATE_HANDLES.md"
        candidate.write_bytes(source_bytes if edited == draft_text else edited.encode("utf-8"))
        report.update(edited_draft=str(candidate), applied=applied, skipped=skipped,
            changes_proposed=len(parsed["changes"]), unresolved_questions=parsed["unresolved_questions"],
            draft_chars_before=len(draft_text), draft_chars_after=len(edited))
        if skipped:
            report["pending_problems"].append({"code": "article_edit_targets_skipped", "details": skipped})
        else:
            report["status"] = "no_change" if parsed["no_change"] else "edited"
        if identity_catalogs is not None:
            from .delivery_citations import build_delivery_citation_map, run_figures_citations_stage
            before = _numbering_projection(draft_text)
            after = _numbering_projection(edited)
            old_map = build_delivery_citation_map(final_handle_draft=before, identity_catalogs=identity_catalogs)
            new_map = build_delivery_citation_map(final_handle_draft=after, identity_catalogs=identity_catalogs)
            old_ids, new_ids = set(old_map["references_order"]), set(new_map["references_order"])
            report["source_identity_changes"] = {"removed": sorted(old_ids - new_ids),
                "added": sorted(new_ids - old_ids), "before_count": len(old_ids), "after_count": len(new_ids)}
            projection = out_dir / "NUMBERING_INPUT_HANDLES.md"
            projection.write_text(after, encoding="utf-8", newline="\n")
            numbered = run_figures_citations_stage(final_draft_path=projection,
                identity_catalogs=identity_catalogs, out_dir=out_dir / "numbered")
            report["numbering"] = numbered
            if numbered["status"] != "complete":
                report["status"] = "pending"
                report["pending_problems"].append({"code": "article_edit_citations_pending"})
        _write(report_path, report)
        files = {path.relative_to(out_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (original_path, candidate, out_dir / "EDIT_PROPOSALS.json",
                         step / "RAW_RESPONSE.json", step / "REQUEST.json") if path.is_file()}
        if "numbering" in report:
            for key in ("reader_draft", "handles_draft", "references_path", "mapping_path"):
                path = Path(report["numbering"][key])
                files[path.relative_to(out_dir).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        _write(seal_path, {"report_sha256": _hash(report), "files": files})
        return report


def _numbering_projection(text: str) -> str:
    """Exclude the old bibliography from the new final citation inventory."""
    import re
    return re.sub(r"^#{1,3}\s+(?:参考文献|References\b|Bibliography\b)[^\n]*\n(?:(?!^#{1,3}\s).)*",
                  "", text, flags=re.MULTILINE | re.DOTALL | re.IGNORECASE).rstrip() + "\n"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
