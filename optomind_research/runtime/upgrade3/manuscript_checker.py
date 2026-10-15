"""Two-round, snapshot-bound checking of an assembled HANDLE manuscript.

This is an execution/provenance contract, not a scientific entailment verifier.
It never calls the BODY author, changes identity catalogs or numbers references.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from typing import Any, Mapping

from .post_body_revision_contracts import (
    RevisionContractError, apply_patches, normalize_case, resolve_target,
    sha256_text, validate_patch,
)
from .delivery_citations import BRACKET_RE, HANDLE_TOKEN_RE, build_delivery_citation_map
from .json_format_recovery import JsonFormatError, recover_json_format
from .review_unit_writer import _fenced_line_indices
from .writer_candidates import DEFAULT_PROFILE, _output_lock

SCHEMA = "optomind.manuscript_checker.v1"
PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "manuscript_checker.md"

_FORMAT_REPAIR_ENVELOPE_FIELDS = (
    "content", "call", "call_id", "model", "returned_model", "requested_model",
    "request_id", "provider_request_id", "finish_reason", "complete",
    "usage", "effective_request", "elapsed_seconds", "execution_mode",
    "raw_response_sha256", "raw_stream_sha256", "provider_raw_response_sha256",
    "response_sha256", "response_payload_sha256", "sse_payload_sha256",
)


class ManuscriptCheckerError(ValueError):
    """A source, cache, protocol or selected-artifact integrity failure."""


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _write_bytes(path: Path, value: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with tmp.open("wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _write(path, value):
    _write_bytes(Path(path), (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def _read(path):
    return json.loads(Path(path).read_bytes().decode("utf-8"))


def _write_format_recovery(step, raw_path, audits):
    if not audits:
        return None
    path = Path(step) / "FORMAT_RECOVERY.json"
    _write(path, {"schema": "optomind.manuscript_checker.format_recovery.v1",
                  "raw_response_path": str(raw_path), "audits": audits})
    return path


def _format_repair_response(raw):
    """Keep only the provider envelope needed to repair a malformed response.

    Provider responses retain their complete content, while transport/raw
    wrappers and duplicated normalized or reasoning payloads stay local in
    RAW_RESPONSE.json. Non-provider fixtures (strings or mappings without
    ``content``) are returned unchanged for backwards compatibility.
    """
    if not isinstance(raw, Mapping) or "content" not in raw:
        return raw
    return {key: deepcopy(raw[key]) for key in _FORMAT_REPAIR_ENVELOPE_FIELDS if key in raw}


def _tokens(text):
    return [token for bracket in BRACKET_RE.finditer(text)
            for token in HANDLE_TOKEN_RE.findall(bracket.group(1))]


def _material_case(text, material_records):
    records = deepcopy(material_records or [])
    if isinstance(records, Mapping):
        records = [{**(dict(value) if isinstance(value, Mapping) else {"text": value}),
                    "material_id": key} for key, value in records.items()]
    if not isinstance(records, list):
        raise ManuscriptCheckerError("materials_must_be_list_or_mapping")
    prepared = []
    for index, record in enumerate(records, 1):
        if not isinstance(record, Mapping):
            raise ManuscriptCheckerError("invalid_material_record")
        if str(record.get("material_id", record.get("id", ""))).startswith("snapshot:"):
            raise ManuscriptCheckerError("reserved_evidence_id")
        record = dict(record)
        record.setdefault("material_id", record.get("id") or f"M{index:04d}")
        handles = record.get("source_handles", [])
        if not isinstance(handles, list):
            raise ManuscriptCheckerError("invalid_source_handles")
        if record.get("source_handle"):
            handles = list(handles) + [record["source_handle"]]
        record["source_handles"] = list(dict.fromkeys(handles))
        prepared.append(record)
    base = normalize_case({"draft_text": text, "materials": prepared})
    for block in base["blocks"]:
        ident = f"snapshot:{base['base_sha256']}:{block['block_id']}"
        base["materials"][ident] = {"text": block["text"], "source_handles": _tokens(block["text"]),
                                    "title": "Current manuscript block", "summary": ""}
    return base


def build_checker_messages(*, draft_text, identity_catalogs=None, material_records=None,
                           figure_assets=None, chapter_roles=None, language="zh",
                           round_number=1, feedback=None):
    """Single full-text block view; draft blocks are not duplicated as materials."""
    case = _material_case(draft_text, material_records)
    blocks = [{"block_id": b["block_id"], "text": b["text"],
               "evidence_id": f"snapshot:{case['base_sha256']}:{b['block_id']}"}
              for b in case["blocks"]]
    payload = {"snapshot_id": case["base_sha256"], "round": round_number,
        "language": language, "blocks": blocks, "chapter_roles": chapter_roles or [],
        "identity_catalogs": identity_catalogs or [],
        "materials": {k: v for k, v in case["materials"].items() if not k.startswith("snapshot:")},
        "pending_figure_assets": figure_assets or [], "feedback": feedback,
        "task": "Read the complete manuscript and propose independent local corrections." if round_number == 1
                else "FORMAT REPAIR: repair only the original malformed response; no edit IDs existed, so omit edit_id. Do not discover new issues." if (feedback or {}).get("format_repair")
                else "Repair only pending proposals, using this current snapshot. Do not discover new issues."}
    return [{"role": "system", "content": PROMPT_PATH.read_bytes().decode("utf-8")},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)}]


def _decode(raw, recovery_audit=None):
    if isinstance(raw, Mapping) and "content" in raw:
        if raw.get("complete") is False or raw.get("finish_reason", "stop") != "stop":
            raise ManuscriptCheckerError("incomplete_response")
        raw = raw["content"]
    if isinstance(raw, str):
        recovery_text = raw
        text = raw.strip()
        if text.startswith("```"):
            match = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.S)
            if not match:
                raise ManuscriptCheckerError("invalid_json_fence")
            text = match.group(1)
        try:
            raw = json.loads(text)
        except ValueError as exc:
            try:
                raw, audit = recover_json_format(recovery_text)
            except (JsonFormatError, ValueError):
                raise ManuscriptCheckerError("invalid_json") from exc
            if audit is not None and recovery_audit is not None:
                recovery_audit.append(audit)
    if not isinstance(raw, Mapping):
        raise ManuscriptCheckerError("response_not_object")
    if set(raw) - {"snapshot_id", "changes", "unresolved_questions"}:
        raise ManuscriptCheckerError("unknown_root_fields")
    if not isinstance(raw.get("changes"), list) or not isinstance(raw.get("unresolved_questions", []), list):
        raise ManuscriptCheckerError("invalid_response_rows")
    return dict(raw)


def _numeric_signature(text):
    # Citation identities have their own stronger, namespace-aware comparison.
    text = BRACKET_RE.sub(lambda m: "" if HANDLE_TOKEN_RE.search(m.group(1)) else m.group(0), text)
    # A hyphen/en dash between numeric endpoints is range typography. Unary
    # minus signs and exponent signs keep their original spelling and value.
    text = re.sub(r"(?<=\d)(?:[-–]|[ \t]+[-–][ \t]+)(?=[+\-−]?\d)", "-", text)
    return [re.sub(r"\s+", "", n) for n in re.findall(r"[+\-−–]?\s*\d+(?:[,.]\d+)*(?:[eE][+\-−]?\d+)?(?:\s*(?:%|‰|[A-Za-zµμ]+))?", text)]


def _numeric_signatures(before, after):
    old_lines, new_lines = before.splitlines(keepends=True), after.splitlines(keepends=True)
    if len(old_lines) == len(new_lines):
        code_lines = _fenced_line_indices(before) | _fenced_line_indices(after)
        for index, (old, new) in enumerate(zip(old_lines, new_lines)):
            if index in code_lines:
                continue
            heading = r" {0,3}#{1,6}[ \t]+([^\r\n]*)(\r?\n)?"
            old_heading, new_heading = re.fullmatch(heading, old), re.fullmatch(heading, new)
            if old_heading and new_heading:
                numbered = re.fullmatch(r"\d+(?:\.\d+)+\.?[ \t]+(.+)", new_heading[1])
                # Only an added dotted section prefix may disappear from the
                # comparison; every character of the actual title must remain.
                if numbered and numbered[1] == old_heading[1]:
                    new_lines[index] = new[:new_heading.start(1)] + numbered[1] + (new_heading[2] or "")
        after = "".join(new_lines)
    return _numeric_signature(before), _numeric_signature(after)


def _editorial_number_signature(case, target, replacement):
    block = next(b for b in case["blocks"] if b["block_id"] == target["block_id"])
    start, end = target["start"] - block["start"], target["end"] - block["start"]
    return _numeric_signatures(block["text"], block["text"][:start] + replacement + block["text"][end:])


def _math_signature(text):
    return [re.sub(r"\s+", "", re.sub(r"^\$\$?|\$\$?$|^\\[\[(]|\\[\])]$", "", s)) for s in re.findall(
        r"\$\$.*?\$\$|(?<!\\)\$[^$\n]+(?<!\\)\$|\\\[.*?\\\]|\\\(.*?\\\)", text, re.S)]


def _structure_errors(text):
    errors = Counter()
    fences = []
    prose_lines = []
    for line in text.splitlines():
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if match:
            token = match.group(1)
            if fences and token[0] == fences[-1][0] and len(token) >= len(fences[-1]):
                fences.pop()
            elif not fences:
                fences.append(token)
        elif not fences and not line.startswith(("    ", "\t")):
            prose_lines.append(line)
    errors["unclosed_fence"] = len(fences)
    prose = re.sub(r"(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)", "", "\n".join(prose_lines))
    errors["unbalanced_display_dollars"] = len(re.findall(r"(?<!\\)(?:\\\\)*\$\$", prose)) % 2
    errors["unbalanced_display_math"] = abs(text.count(r"\[") - text.count(r"\]"))
    errors["unbalanced_inline_math"] = abs(text.count(r"\(") - text.count(r"\)"))
    errors["unbalanced_dollars"] = len(re.findall(r"(?<!\\)\$", text)) % 2
    for block in re.split(r"\r?\n\s*\r?\n", text):
        rows = [line for line in block.splitlines() if re.match(r"^\s*\|", line)]
        counts = [len(re.findall(r"(?<!\\)\|", row)) for row in rows]
        if len(counts) > 1:
            errors["table_column_mismatch"] += sum(c != max(set(counts), key=counts.count) for c in counts)
    bracket_ranges = [(m.start(), m.end()) for m in BRACKET_RE.finditer(text)]
    for token in HANDLE_TOKEN_RE.finditer(text):
        # Bare catalog handles in a bibliography are legal; an orphan bracket
        # immediately beside a handle is not a well-formed citation token.
        if not any(a <= token.start() and token.end() <= b for a, b in bracket_ranges):
            left, right = text[max(0, token.start() - 1):token.start()], text[token.end():token.end() + 1]
            if left == "[" or right == "]":
                errors["orphan_citation_delimiter"] += 1
    for match in BRACKET_RE.finditer(text):
        body = match.group(1)
        if re.search(r"(?:^|[\s,;]|::)P\d", body):
            residue = HANDLE_TOKEN_RE.sub("", body)
            if residue.strip(" ,;，；\t\n"):
                errors["malformed_citation"] += 1
    return errors


def _guard(before, after):
    markers = lambda s: re.findall(r"<!--.*?-->", s, re.S)
    if markers(before) != markers(after):
        raise ManuscriptCheckerError("managed_markers_changed")
    old, new = _structure_errors(before), _structure_errors(after)
    worsened = [key for key, count in new.items() if count > old[key]]
    if worsened:
        raise ManuscriptCheckerError("structure_worsened:" + ",".join(sorted(worsened)))


def _resolve_citations(text, catalogs):
    return build_delivery_citation_map(final_handle_draft=text, identity_catalogs=catalogs or [])


def _validate_change(case, row, edit_id, catalogs):
    if not isinstance(row, Mapping):
        raise ManuscriptCheckerError("proposal_not_object")
    allowed = {"edit_id", "target_block_id", "operation", "original_text", "replacement_text",
               "reason", "kind", "evidence_ids", "depends_on"}
    if set(row) - allowed:
        raise ManuscriptCheckerError("unknown_proposal_fields")
    for field in ("target_block_id", "original_text", "reason"):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise ManuscriptCheckerError("missing_" + field)
    if not isinstance(row.get("operation"), str) or row["operation"] not in {"replace", "remove"}:
        raise ManuscriptCheckerError("operation_not_allowed")
    if row.get("depends_on"):
        raise ManuscriptCheckerError("dependent_changes_not_supported")
    replacement = row.get("replacement_text", "")
    if not isinstance(replacement, str):
        raise ManuscriptCheckerError("replacement_not_text")
    if row["operation"] == "remove" and replacement:
        raise ManuscriptCheckerError("remove_has_replacement")
    if row["operation"] == "replace" and not replacement.strip():
        raise ManuscriptCheckerError("empty_replacement")
    kind = row.get("kind", "editorial")
    if not isinstance(kind, str) or kind not in {"editorial", "scientific"}:
        raise ManuscriptCheckerError("unknown_kind")
    ids = row.get("evidence_ids", [])
    if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
        raise ManuscriptCheckerError("invalid_evidence_ids")
    target = resolve_target(case, row)
    # A genuinely multi-block manuscript cannot be rewritten by a single patch:
    # resolve_target already limits its anchor to one real block. For a long
    # degenerate one-block input, whole-document replacement is still outside
    # this stage's local-edit remit; tiny synthetic/single-sentence drafts remain
    # useful for tests and ordinary one-sentence local corrections.
    if (len(case["blocks"]) == 1 and len(case["draft_text"]) > 2000
            and row["original_text"].strip() == case["draft_text"].strip()):
        raise ManuscriptCheckerError("whole_manuscript_rewrite_not_allowed")
    block = next(b for b in case["blocks"] if b["block_id"] == target["block_id"])
    start, end = target["start"] - block["start"], target["end"] - block["start"]
    affected = block["text"][:start] + replacement + block["text"][end:]
    _guard(block["text"], affected)
    # Semantic-risk checks are on the complete affected block, including tokens
    # straddling the tiny old/new fragments. Reason alone never supplies evidence.
    strip_math_delimiters = lambda value: re.sub(r"(?<!\\)\$|\\[\[\]() ]", "", value)
    math_changed = (_math_signature(block["text"]) != _math_signature(affected)
                    and strip_math_delimiters(block["text"]) != strip_math_delimiters(affected))
    numeric_before, numeric_after = _numeric_signatures(block["text"], affected)
    sensitive = (numeric_before != numeric_after
                 or math_changed
                 or _tokens(block["text"]) != _tokens(affected))
    if sensitive and kind != "scientific":
        raise ManuscriptCheckerError("meaning_change_requires_scientific_evidence")
    issue = {**dict(row), "issue_id": edit_id, "kind": kind, "evidence_ids": ids}
    # Namespace validation uses the same canonical resolver as final delivery.
    old_map, new_map = _resolve_citations(block["text"], catalogs), _resolve_citations(affected, catalogs)
    if set(new_map["unknown_handles"]) - set(old_map["unknown_handles"]):
        raise ManuscriptCheckerError("unknown_introduced_citation")
    introduced_tokens = set(_tokens(affected)) - set(_tokens(block["text"]))
    supported = set()
    for ident in ids:
        material = case["materials"].get(ident, {})
        supported.update(material.get("source_handles", []))
        supported.update(_tokens(material.get("text", "")))
    supported_map = _resolve_citations(" ".join("[" + h + "]" for h in supported), catalogs)
    def evidence_identity(token, detail):
        canonical = detail["canonical"]
        # Delivery qualifies weak fallback identities by namespace and the
        # catalog row's primary handle. Thus explicit aliases in the SAME row
        # share identity, while equal P numbers in different namespaces do not.
        return canonical
    supported_identities = {evidence_identity(token, detail)
                            for token, detail in supported_map["token_details"].items()}
    for token in introduced_tokens:
        detail = new_map["token_details"].get(token)
        if detail is None or evidence_identity(token, detail) not in supported_identities:
            raise ManuscriptCheckerError("citation_not_supported_by_selected_evidence:" + token)
    # The inherited contract intentionally uses bare P handles. Adapt its
    # evidence handle view only AFTER the full namespace check above succeeded.
    adapted = deepcopy(case)
    for ident in ids:
        if ident in adapted["materials"]:
            material = adapted["materials"][ident]
            exact_handles = list(material.get("source_handles", [])) + _tokens(material.get("text", ""))
            material_map = _resolve_citations(" ".join("[" + h + "]" for h in exact_handles), catalogs)
            material_identities = {evidence_identity(token, detail)
                                   for token, detail in material_map["token_details"].items()}
            # Expand only aliases already authorized by this material's exact
            # canonical identity. This is an adapter for the inherited bare-P
            # evidence guard, never fuzzy matching or catalog mutation.
            authorized_aliases = [token for token in introduced_tokens
                if evidence_identity(token, new_map["token_details"][token]) in material_identities]
            material["source_handles"] = sorted({h.split("::")[-1] for h in exact_handles + authorized_aliases})
    patch = validate_patch(adapted, issue, {"replacement_text": replacement},
        editorial_number_signature=_editorial_number_signature)
    # apply_patches revalidates against this same adapted material view.
    return patch, adapted, {"reason": row["reason"], "kind": kind, "evidence_ids": ids,
                           "citation_before": old_map["token_details"], "citation_after": new_map["token_details"]}


def _failure(case, edit_id, proposal, code):
    row = dict(proposal) if isinstance(proposal, Mapping) else {"invalid_proposal": proposal}
    block = next((b for b in case["blocks"] if b["block_id"] == row.get("target_block_id")), None)
    original = row.get("original_text")
    count = len(re.findall("(?=" + re.escape(original) + ")", block["text"])) if block and isinstance(original, str) and original else 0
    return {"edit_id": edit_id, "code": code, "proposal": row, "match_count": count,
            "original_target_block": dict(block) if block else None}


def execute_checker_response(*, draft_text, response, identity_catalogs=None,
                             material_records=None, pending_ids=None, pending_bindings=None, round_number=1,
                             decode_audit=None):
    """Pure strict execution; independent valid proposals survive invalid siblings."""
    case = _material_case(draft_text, material_records)
    parsed = _decode(response, recovery_audit=decode_audit)
    if parsed.get("snapshot_id") != case["base_sha256"]:
        raise ManuscriptCheckerError("stale_snapshot")
    rows = parsed["changes"]
    allowed_ids = set(pending_ids or [])
    counts = Counter(str(row.get("edit_id", "")) for row in rows if isinstance(row, Mapping))
    valid, pending, extras, adapters = [], [], {}, {}
    for index, row in enumerate(rows, 1):
        edit_id = f"E{index:04d}" if round_number == 1 else str(row.get("edit_id", "")) if isinstance(row, Mapping) else ""
        try:
            if round_number == 1 and isinstance(row, Mapping) and "edit_id" in row:
                raise ManuscriptCheckerError("first_round_edit_id_is_program_assigned")
            if round_number > 1 and (edit_id not in allowed_ids or counts[edit_id] != 1):
                raise ManuscriptCheckerError("unknown_or_duplicate_pending_id")
            if round_number > 1 and pending_bindings is not None:
                allowed_blocks = pending_bindings.get(edit_id, [])
                if row.get("target_block_id") not in allowed_blocks:
                    raise ManuscriptCheckerError("repair_target_outside_pending_scope")
            patch, adapted, extra = _validate_change(case, row, edit_id, identity_catalogs or [])
            valid.append(patch)
            extras[edit_id] = extra
            adapters[edit_id] = adapted
        except (ManuscriptCheckerError, RevisionContractError) as exc:
            pending.append(_failure(case, edit_id, row, str(exc)))
    conflict = set()
    for i, left in enumerate(valid):
        for right in valid[i + 1:]:
            if max(left["start"], right["start"]) < min(left["end"], right["end"]):
                conflict.update((left["issue_id"], right["issue_id"]))
    accepted = []
    for patch in valid:
        if patch["issue_id"] in conflict:
            pending.append(_failure(case, patch["issue_id"], rows[int(patch["issue_id"][1:]) - 1]
                if round_number == 1 else next(r for r in rows if isinstance(r, Mapping) and r.get("edit_id") == patch["issue_id"]), "overlap"))
        else:
            accepted.append(patch)
    # Merge the adapter's bare-handle evidence projection for all accepted items.
    adapted = deepcopy(case)
    for patch in accepted:
        for ident in patch["evidence_ids"]:
            material = adapted["materials"][ident]
            current_handles = material.get("source_handles", [])
            validated_handles = adapters[patch["issue_id"]]["materials"][ident]["source_handles"]
            material["source_handles"] = sorted(set(current_handles) | set(validated_handles))
    result = apply_patches(adapted, accepted, editorial_number_signature=_editorial_number_signature)
    _guard(draft_text, result["draft_text"])
    receipts = [{**patch, "edit_id": patch["issue_id"], **extras[patch["issue_id"]],
                 "input_sha256": case["base_sha256"], "output_sha256": sha256_text(result["draft_text"])} for patch in accepted]
    if round_number > 1:
        addressed = {r["edit_id"] for r in receipts} | {p["edit_id"] for p in pending}
        for ident in sorted(allowed_ids - addressed):
            pending.append({"edit_id": ident, "code": "repair_omitted_pending_id", "proposal": {}})
    return {"draft_text": result["draft_text"], "applied": receipts, "pending": pending,
            "unresolved_questions": parsed.get("unresolved_questions", [])}


def _repair_feedback(old_text, current_text, pending, applied):
    current = _material_case(current_text, [])
    candidates = []
    for failure in pending:
        row = deepcopy(failure)
        old = row.pop("original_target_block", None)
        mapped = None
        if old is not None:
            touching = [p for p in applied if max(old["start"], p["start"]) < min(old["end"], p["end"])]
            if not touching:
                delta = sum(len(p["replacement_text"]) - (p["end"] - p["start"])
                            for p in applied if p["end"] <= old["start"])
                matches = [b for b in current["blocks"] if b["start"] == old["start"] + delta and b["text"] == old["text"]]
                if len(matches) == 1:
                    mapped = matches[0]["block_id"]
        allowed_blocks = []
        if old is not None:
            shift_before = sum(len(p["replacement_text"]) - (p["end"] - p["start"])
                               for p in applied if p["end"] <= old["start"])
            shift_through = sum(len(p["replacement_text"]) - (p["end"] - p["start"])
                                for p in applied if p["end"] <= old["end"])
            begin, end = old["start"] + shift_before, old["end"] + shift_through
            allowed_blocks = [b["block_id"] for b in current["blocks"]
                              if max(begin, b["start"]) < min(end, b["end"])]
        else:
            original = row.get("proposal", {}).get("original_text")
            if isinstance(original, str) and original:
                allowed_blocks = [b["block_id"] for b in current["blocks"] if original in b["text"]]
        row["allowed_current_block_ids"] = allowed_blocks
        row["current_target_block_id"] = mapped
        row["target_migration"] = "exact_untouched_interval" if mapped else "read_current_blocks_and_copy_actual_anchor"
        # The actual complete current blocks occur once in the primary view.
        candidates.append(row)
    return {"pending_edit_ids": [p["edit_id"] for p in pending], "failures": candidates,
            "applied_receipts": applied, "instruction": "Only pending IDs; successful edits must not be resent."}


def _fixture_for(source, number):
    if source is None:
        return None
    if isinstance(source, (str, Path)):
        source = _read(source)
    if isinstance(source, list):
        return source[number - 1] if len(source) >= number else None
    if isinstance(source, Mapping):
        if "changes" in source or "content" in source:
            return source if number == 1 else None
        return source.get(f"round_{number}", source.get(str(number)))
    raise ManuscriptCheckerError("invalid_fixture_responses")


def run_manuscript_checker(*, draft_path, out_dir, identity_catalogs=None,
                           material_records=None, figure_assets=None, chapter_roles=None,
                           language="zh", fixture_responses=None, recordings=None,
                           client_factory=None, run=False, token_counter=None, profile=None,
                           retry_failed=False, enabled=True, max_model_rounds=2):
    """Check selected 03 manuscript; cache resumes never reset logical rounds.

    Fixtures must contain the actual snapshot hash, just like live responses.
    A callable fixture(message payload, round_number) is supported only for
    clearly labeled offline fixtures, never as a production evidence source.
    """
    if max_model_rounds not in (1, 2):
        raise ManuscriptCheckerError("max_model_rounds_must_be_one_or_two")
    source, output = Path(draft_path).resolve(), Path(out_dir).resolve()
    if not source.is_file():
        raise ManuscriptCheckerError("source_manuscript_missing")
    original_bytes = source.read_bytes()
    text = original_bytes.decode("utf-8")
    digest = hashlib.sha256(original_bytes).hexdigest()
    effective = {**DEFAULT_PROFILE, **dict(profile or {})}
    first_messages = build_checker_messages(draft_text=text, identity_catalogs=identity_catalogs,
        material_records=material_records, figure_assets=figure_assets, chapter_roles=chapter_roles, language=language) if enabled else []
    identity = {"schema": SCHEMA, "source_path": str(source), "source_sha256": digest,
        "first_messages_sha256": _hash(first_messages), "profile": effective,
        "max_model_rounds": max_model_rounds, "enabled": enabled,
        "response_source": "fixture" if fixture_responses is not None else "recordings" if recordings is not None else "provider"}
    # Bind fixture payloads too. Callable fixtures exist solely for synthetic
    # tests; their saved RAW becomes the authoritative immutable response.
    if fixture_responses is not None and not callable(fixture_responses):
        identity["fixtures_sha256"] = _hash(_read(fixture_responses) if isinstance(fixture_responses, (str, Path)) else fixture_responses)
    if recordings is not None:
        identity["recordings_sha256"] = _hash(_read(recordings) if isinstance(recordings, (str, Path)) else recordings)
    with _output_lock(output):
        identity_path = output / "CHECKER_IDENTITY.json"
        if identity_path.exists() and _read(identity_path) != identity:
            raise ManuscriptCheckerError("checker_input_identity_changed:use_new_output_directory")
        _write(identity_path, identity)
        snapshot = output / "ORIGINAL_HANDLES.md"
        if snapshot.exists() and snapshot.read_bytes() != original_bytes:
            raise ManuscriptCheckerError("original_snapshot_integrity_failure")
        if not snapshot.exists():
            _write_bytes(snapshot, original_bytes)
        report_path, seal_path = output / "CHECKER_REPORT.json", output / "CHECKER_RESULT_SEAL.json"
        if seal_path.exists():
            seal = _read(seal_path)
            if not report_path.exists() or _hash(_read(report_path)) != seal.get("report_sha256"):
                raise ManuscriptCheckerError("cached_report_integrity_failure")
            for name, wanted in seal["files"].items():
                path = output / name
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != wanted:
                    raise ManuscriptCheckerError("cached_artifact_integrity_failure:" + name)
            saved = _read(report_path)
            selected = saved.get("selected_manuscript")
            if selected and (not Path(selected["path"]).is_file() or hashlib.sha256(Path(selected["path"]).read_bytes()).hexdigest() != selected["sha256"]):
                raise ManuscriptCheckerError("selected_manuscript_integrity_failure")
            saved.update(model_calls=0, external_requests=0, provider_calls=0, cache_hit=True)
            return saved
        report = {"schema": SCHEMA, "stage": "manuscript_checker", "status": "pending",
            "source_draft": str(source), "source_sha256": digest, "original_snapshot": str(snapshot),
            "selected_manuscript": None, "applied": [], "pending": [], "diagnostics": [],
            "unresolved_questions": [], "model_calls": 0, "external_requests": 0,
            "logical_rounds": 0, "provider_calls": 0, "max_model_rounds": max_model_rounds,
            "scientific_acceptance": False, "cache_hit": False,
            "proposals_source": identity["response_source"], "rounds": []}
        current, selected_path, pending, feedback = text, source, [], None
        completed_response = False

        def current_selection():
            if source.read_bytes() != original_bytes:
                raise ManuscriptCheckerError("source_changed_during_checker")
            if selected_path.read_bytes() != current.encode("utf-8"):
                raise ManuscriptCheckerError("selected_manuscript_integrity_failure")
            return {"path": str(selected_path), "sha256": sha256_text(current),
                "status": report["status"], "provenance": {"source_path": str(source), "source_sha256": digest,
                    "checker_report": str(report_path), "applied_edit_ids": [p["edit_id"] for p in report["applied"]],
                    "body_preservation_scope": "03 assembly only; checker local changes are separately receipted"}}

        if enabled:
            for number in range(1, max_model_rounds + 1):
                messages = first_messages if number == 1 else build_checker_messages(
                    draft_text=current, identity_catalogs=identity_catalogs, material_records=material_records,
                    figure_assets=figure_assets, chapter_roles=chapter_roles, language=language,
                    round_number=number, feedback=feedback)
                step = output / f"round_{number}"
                step.mkdir(exist_ok=True)
                raw_path, request_path = step / "RAW_RESPONSE.json", step / "REQUEST.json"
                raw = None
                calls, state = 0, "missing_response_source"
                if fixture_responses is not None or recordings is not None:
                    request = {"messages_sha256": _hash(messages), "profile": effective}
                    if request_path.exists() and _read(request_path) != request:
                        raise ManuscriptCheckerError("round_request_identity_changed")
                    if raw_path.exists() and not request_path.exists():
                        raise ManuscriptCheckerError("raw_without_request_identity")
                    _write(request_path, request)
                    _write(step / "MESSAGES.json", messages)
                    if raw_path.exists():
                        raw, state = _read(raw_path), "cache_hit"
                    else:
                        raw = fixture_responses(json.loads(messages[1]["content"]), number) if callable(fixture_responses) else _fixture_for(fixture_responses if fixture_responses is not None else recordings, number)
                        if raw is not None:
                            _write(raw_path, raw)
                            state = "fixture" if fixture_responses is not None else "recording"
                else:
                    from .unit_realization import _cached_call
                    if raw_path.exists() and not request_path.exists():
                        raise ManuscriptCheckerError("raw_without_request_identity")
                    if client_factory is not None or request_path.exists() or raw_path.exists():
                        raw, calls, state = _cached_call(step, messages, effective, client_factory,
                            bool(run and client_factory is not None), token_counter, retry_failed=retry_failed)
                    else:
                        _write(step / "MESSAGES.json", messages)
                report["provider_calls"] += calls
                report["model_calls"] += calls
                execution_mode = (raw.get("execution_mode") if isinstance(raw, Mapping) else None) or getattr(client_factory, "execution_mode", "unverified")
                report["execution_mode"] = identity["response_source"] if identity["response_source"] != "provider" else execution_mode
                report["external_requests"] += calls if execution_mode not in {"fixture", "recording", "injected"} else 0
                report["rounds"].append({"round": number, "state": state, "response_path": str(raw_path),
                    "input_sha256": sha256_text(current), "messages_path": str(step / "MESSAGES.json")})
                if raw is None:
                    report["preview_messages_path"] = str(step / "MESSAGES.json")
                    report["diagnostics"].append({"code": state, "round": number})
                    report["pending"] = pending
                    if completed_response:
                        report["selected_manuscript"] = current_selection()
                    _write(report_path, report)
                    return report
                report["logical_rounds"] = number
                if identity["response_source"] == "provider" and (not isinstance(raw, Mapping)
                        or raw.get("complete") is not True or raw.get("finish_reason") != "stop"):
                    report["diagnostics"].append({"code": "incomplete_provider_response", "round": number})
                    report["pending"] = pending
                    if completed_response:
                        report["selected_manuscript"] = current_selection()
                    _write(report_path, report)
                    return report
                previous = current
                decode_audit = []
                try:
                    returned_model = (raw.get("returned_model") or raw.get("model")) if isinstance(raw, Mapping) else None
                    if returned_model and returned_model != effective["model"]:
                        raise ManuscriptCheckerError("returned_model_mismatch")
                    # A malformed root response gets one bounded format repair;
                    # no proposal IDs existed yet, so the repair may submit its
                    # original intended changes, not a new discovery pass.
                    format_repair = number > 1 and pending and all(p["code"].startswith("response:") for p in pending)
                    result = execute_checker_response(draft_text=current, response=raw,
                        identity_catalogs=identity_catalogs, material_records=material_records,
                        pending_ids=[p["edit_id"] for p in pending],
                        pending_bindings={f["edit_id"]: f.get("allowed_current_block_ids", [])
                                          for f in (feedback or {}).get("failures", [])} if number > 1 and not format_repair else None,
                        round_number=1 if format_repair else number, decode_audit=decode_audit)
                    completed_response = True
                    current = result["draft_text"]
                    pending = result["pending"]
                    report["applied"].extend(result["applied"])
                    report["unresolved_questions"].extend(result["unresolved_questions"])
                    if result["applied"]:
                        selected_path = step / "CANDIDATE_HANDLES.md"
                        _write_bytes(selected_path, current.encode("utf-8"))
                    recovery_path = _write_format_recovery(step, raw_path, decode_audit)
                    receipt = {**result, "draft_text": None,
                        "input_sha256": sha256_text(previous), "output_sha256": sha256_text(current),
                        "selected_path": str(selected_path)}
                    if recovery_path is not None:
                        receipt["format_recovery_path"] = str(recovery_path)
                    _write(step / "EXECUTION_RECEIPT.json", receipt)
                    feedback = _repair_feedback(previous, current, pending, result["applied"])
                except (ManuscriptCheckerError, RevisionContractError) as exc:
                    _write_format_recovery(step, raw_path, decode_audit)
                    if number > 1 and pending:
                        report["diagnostics"].append({"code": "repair_response_invalid", "error": str(exc)})
                    else:
                        pending = [{"edit_id": "RESPONSE", "code": "response:" + str(exc), "proposal": {}}]
                    feedback = {"format_repair": True, "pending_edit_ids": [], "failures": pending,
                        "invalid_response": _format_repair_response(raw),
                        "instruction": "Repair the original response format or snapshot binding only. Copy the actual snapshot. Do not discover a new set of issues."}
                    _write(step / "RESPONSE_ERROR.json", {"error": str(exc)})
                _write(step / "FEEDBACK.json", feedback)
                if not pending:
                    break
        report["pending"] = pending
        if source.read_bytes() != original_bytes:
            raise ManuscriptCheckerError("source_changed_during_checker")
        if selected_path.read_bytes() != current.encode("utf-8"):
            raise ManuscriptCheckerError("selected_manuscript_integrity_failure")
        if enabled and not completed_response:
            report["diagnostics"].append({"code": "no_valid_complete_response"})
            _write(report_path, report)
            return report
        report["status"] = "disabled" if not enabled else "complete_with_diagnostics" if pending or report["unresolved_questions"] else "edited" if report["applied"] else "no_change"
        before_citations = _resolve_citations(text, identity_catalogs)
        after_citations = _resolve_citations(current, identity_catalogs)
        report["source_identity_changes"] = {
            "before": before_citations["token_details"], "after": after_citations["token_details"],
            "removed_tokens": sorted(set(_tokens(text)) - set(_tokens(current))),
            "added_tokens": sorted(set(_tokens(current)) - set(_tokens(text))),
            "unknown_handles": after_citations["unknown_handles"]}
        report["selected_manuscript"] = current_selection()
        report["diagnostics"].extend({"code": "pending_edit", "edit_id": p["edit_id"], "reason": p["code"]} for p in pending)
        _write(report_path, report)
        files = {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in output.rglob("*") if p.is_file() and p.name != ".writer_candidates.lock" and p not in (report_path, seal_path)}
        _write(seal_path, {"report_sha256": _hash(report), "files": files})
        return report
