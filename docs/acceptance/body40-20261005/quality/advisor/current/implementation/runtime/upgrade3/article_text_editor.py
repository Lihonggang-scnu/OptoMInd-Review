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
    changes = [dict(item) for item in (data.get("changes") or [])
               if isinstance(item, Mapping)]
    unresolved = [dict(item) for item in (data.get("unresolved_questions") or [])
                  if isinstance(item, Mapping)]
    for index, change in enumerate(changes):
        missing = [field for field in ("operation", "original_text")
                   if not str(change.get(field) or "").strip()]
        if missing:
            raise TextEditError(f"edit_change_missing_fields:{index}:{','.join(missing)}")
        if change["operation"] not in {"replace", "remove"}:
            raise TextEditError(f"edit_operation_unknown:{index}:{change['operation']}")
        if change["operation"] == "replace" and not str(change.get("replacement_text") or "").strip():
            raise TextEditError(f"edit_replace_without_replacement:{index}")
    return {"changes": changes, "unresolved_questions": unresolved,
            "no_change": not changes}


def apply_text_edits(
    draft_text: str,
    changes: Sequence[Mapping[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply parsed edits deterministically; report every skipped target.

    A change applies only when ``original_text`` occurs exactly once in the
    current text.  Applying twice is a no-op because the first application
    removes the target.  The already-applied check is decided by the TARGET
    text itself: only when the original target is gone and this edit's own
    addition sits where the target used to be is the edit considered applied.
    The addition appearing elsewhere in the document never counts — the same
    phrase can legitimately exist in untouched paragraphs.
    """

    text = draft_text
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for change in changes:
        original = str(change.get("original_text") or "")
        operation = str(change.get("operation") or "")
        replacement = str(change.get("replacement_text") or "")             if operation == "replace" else ""
        count = text.count(original)

        def skip(reason: str) -> None:
            skipped.append({
                "operation": operation,
                "original_text": original[:120],
                "reason": reason,
                "target_block_id": str(change.get("target_block_id") or ""),
            })

        # Already-applied check, decided AT THE TARGET:
        # - append-style replacement (keeps the original as a prefix): the
        #   edit is applied when the original's single occurrence is directly
        #   followed by this edit's own addition;
        # - independent replacement/removal: applied when the original is gone
        #   and the replacement sits in the text.
        # The same phrase appearing elsewhere never counts.
        if count == 1 and operation == "replace" and replacement.startswith(original):
            addition = replacement[len(original):]
            if addition.strip():
                pos = text.index(original)
                if text[pos + len(original):].startswith(addition):
                    skip("already_applied")
                    continue
        elif count == 0 and replacement.strip() and replacement in text:
            skip("already_applied")
            continue
        if count != 1:
            skip(f"target_occurs_{count}_times")
            continue
        text = text.replace(original, replacement, 1)
        applied.append({
            "operation": operation,
            "original_text": original[:120],
            "replacement_text": (replacement[:120] if replacement else ""),
            "reason": str(change.get("reason") or ""),
            "target_block_id": str(change.get("target_block_id") or ""),
        })
    return text, applied, skipped


def run_text_edit_stage(
    *,
    draft_path: str | Path,
    chapter_roles: Sequence[Mapping[str, Any]],
    out_dir: str | Path,
    proposals_fixture_path: str | Path | None = None,
    recordings: Mapping[str, Mapping[str, Any]] | None = None,
    language: str = "zh",
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


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
