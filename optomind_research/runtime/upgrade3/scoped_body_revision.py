"""Fresh full-manuscript reading and exact, issue-atomic block revision.

No upstream author/parser changes; all dispatch/cache/accounting uses the existing
fullbody transport. Byte-identical untouched spans are preserved by offsets.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import time
import uuid
from typing import Any, Mapping

from . import fullbody_writer as fw
from . import writing_evidence as we
from .fullbody_contracts import fullbody_task_catalog, parse_fullbody_response
from .writer_candidates import ROOT, _hash, _write, _read, _output_lock
from .writer_candidates_contracts import CandidateError

SCHEMA_VERSION = "optomind.scoped_body_revision.v1"
PROMPT_ROOT = ROOT / "prompts/evidence_body_writer"


def _write_body(path: Path, body: str) -> None:
    # Binary writing avoids Windows newline translation changing exact spans.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    temporary.write_bytes(body.encode("utf-8"))
    temporary.replace(path)


def index_blocks(body: str, book: Mapping[str, Any], base: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Index exact paragraphs/tables/fences; separators remain outside edit spans.

    Chapter mapping uses exact chapter titles or single-chapter saved segments.
    Unknown ownership stays unknown rather than trusting guessed ordinal labels.
    """
    segments, offset = [], 0
    for segment in (base or {}).get("segments", []):
        text = segment.get("body_markdown", "")
        start = body.find(text, offset) if text else -1
        if start >= 0:
            segments.append((start, start + len(text), segment.get("chapter_ids", [])))
            offset = start + len(text)
    titles = {}
    for chapter in book.get("chapters", []):
        frame = chapter.get("chapter_frame", {})
        for key in ("title", "chapter_title"):
            if isinstance(frame.get(key), str) and frame[key].strip():
                titles[frame[key].strip()] = chapter["chapter_id"]
    spans, start, pos, fence = [], None, 0, None
    for line in body.splitlines(keepends=True):
        stripped = line.strip()
        marker = re.match(r"^(`{3,}|~{3,})", stripped)
        heading = re.match(r"^#{1,6}\s", line) and fence is None
        if start is not None and ((not stripped and fence is None) or heading):
            end = pos
            while end > start and body[end - 1] in "\r\n":
                end -= 1
            spans.append((start, end))
            start = None
        if stripped and start is None:
            start = pos
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
        pos += len(line)
    if start is not None:
        end = len(body)
        while end > start and body[end - 1] in "\r\n":
            end -= 1
        spans.append((start, end))
    blocks, current = [], None
    for start, end in spans:
        text = body[start:end]
        heading = re.match(r"^(#{1,6})\s+([^\r\n]+)", text)
        if heading:
            title = heading[2].strip().rstrip("#").strip()
            # Remove only an explicit chapter-label prefix; the remaining
            # title must still match exactly, never fuzzy/ordinal guessing.
            unnumbered = re.sub(r"^(?:第[一二三四五六七八九十百零〇0-9]+章|(?:Chapter|Ch)\.?\s+[0-9IVXLCDM]+)[\s:：.、-]+", "", title, flags=re.I)
            matches = {titles[value] for value in (title, unnumbered) if value in titles}
            if len(matches) == 1:
                current = matches.pop()
            elif len(heading[1]) == 1:
                current = None
        owners = next((ids for a, b, ids in segments if a <= start and end <= b and len(ids) == 1), [])
        chapter_id = owners[0] if owners else current
        blocks.append({"block_id": f"block_{len(blocks)+1:06d}", "start": start, "end": end,
                       "sha256": fw._text_hash(text), "content": text, "chapter_id": chapter_id,
                       "chapter_mapping": "saved_segment" if owners else "exact_heading" if current else "unknown"})
    return blocks


def reader_parser(blocks, catalog, book):
    known = {b["block_id"] for b in blocks}
    handles = set(book.get("source_identities", {})) | set(book.get("source_aliases", {}))
    def parse(response):
        obj = fw._object(response)
        if not isinstance(obj.get("issues"), list):
            raise CandidateError("scoped_reader_issues_must_be_list")
        valid, pending, seen = [], [], set()
        for i, raw in enumerate(obj["issues"], 1):
            issue = deepcopy(raw) if isinstance(raw, dict) else {"reported_issue": raw}
            ident = issue.get("issue_id")
            errors = []
            if not isinstance(ident, str) or not ident or ident in seen:
                errors.append("invalid_or_duplicate_issue_id")
                ident = f"invalid_issue_{i:06d}"
            issue["issue_id"] = ident
            seen.add(ident)
            for field, universe, required in (("target_block_ids", known, True), ("related_task_ids", set(catalog), False), ("source_handles", handles, False)):
                values = issue.get(field, [])
                if (not isinstance(values, list) or any(not isinstance(x, str) or x not in universe for x in values)
                    or len(values) != len(set(values)) or (required and not values)):
                    errors.append("invalid_" + field)
                else:
                    issue[field] = values
            for field in ("reason", "goal"):
                if not isinstance(issue.get(field), str) or not issue[field].strip():
                    errors.append("missing_" + field)
            (pending if errors else valid).append({**issue, **({"validation_errors": errors} if errors else {})})
        return {**obj, "issues": valid, "pending_issues": pending, "kind": "reader",
                "complete": obj.get("complete") is True and fw._transport_complete(response)}
    return parse


def group_parser(original, blocks, issues):
    """Invalid issue groups never discard unrelated valid groups."""
    index = {b["block_id"]: b for b in blocks}
    expected = {i["issue_id"]: i for i in issues}
    def parse(response):
        obj = fw._object(response)
        if "read_source_handles" in obj or "read_atom_ids" in obj:
            if obj.get("groups") or obj.get("rejected_issues"):
                raise CandidateError("reread_cannot_contain_edits")
            for key in ("read_source_handles", "read_atom_ids"):
                values = obj.get(key, [])
                if not isinstance(values, list) or any(not isinstance(x, str) or not x for x in values) or len(values) != len(set(values)):
                    raise CandidateError("invalid_reread")
                obj[key] = values
            if not obj["read_source_handles"] and not obj["read_atom_ids"]:
                raise CandidateError("empty_reread")
            return {**obj, "kind": "reread", "complete": fw._transport_complete(response)}
        groups, rejected = obj.get("groups", []), obj.get("rejected_issues", [])
        if not isinstance(groups, list) or not isinstance(rejected, list):
            raise CandidateError("groups_and_rejections_must_be_lists")
        accepted, pending, rejections, seen, occupied = [], [], [], set(), set()
        counts = {}
        for group in groups + rejected:
            if isinstance(group, dict) and isinstance(group.get("issue_id"), str):
                counts[group["issue_id"]] = counts.get(group["issue_id"], 0) + 1
        for group in groups:
            ident = group.get("issue_id") if isinstance(group, dict) else None
            errors, local = [], set()
            if not isinstance(ident, str) or ident not in expected:
                pending.append({"issue_id": ident, "validation_errors": ["unknown_issue"]})
                continue
            seen.add(ident)
            if counts[ident] != 1:
                errors.append("duplicate_issue_group")
            if group.get("action") != "replace_block" or group.get("original_body_sha256") != fw._text_hash(original):
                errors.append("action_or_original_body_hash_mismatch")
            replacements = group.get("replacements")
            if not isinstance(replacements, list) or not replacements:
                errors.append("empty_or_invalid_replacements")
                replacements = []
            for patch in replacements:
                bid = patch.get("block_id") if isinstance(patch, dict) else None
                if not isinstance(bid, str) or bid not in index or bid not in expected[ident]["target_block_ids"]:
                    errors.append("unknown_or_out_of_scope_block")
                    continue
                block = index[bid]
                if bid in local or bid in occupied:
                    errors.append("duplicate_or_overlapping_block")
                local.add(bid)
                if (patch.get("original_sha256") != block["sha256"] or
                    fw._text_hash(original[block["start"]:block["end"]]) != block["sha256"] or
                    not isinstance(patch.get("content"), str)):
                    errors.append("original_hash_or_content_mismatch")
            if errors:
                pending.append({"issue_id": ident, "validation_errors": sorted(set(errors))})
            else:
                accepted.append(deepcopy(group))
                occupied.update(local)
        for rejection in rejected:
            ident = rejection.get("issue_id") if isinstance(rejection, dict) else None
            if (not isinstance(ident, str) or ident not in expected or counts.get(ident) != 1 or
                not isinstance(rejection.get("reason"), str) or not rejection["reason"].strip()):
                pending.append({"issue_id": ident, "validation_errors": ["invalid_rejection"]})
            else:
                seen.add(ident)
                rejections.append(deepcopy(rejection))
        pending.extend({"issue_id": ident, "validation_errors": ["issue_not_addressed"]} for ident in expected if ident not in seen)
        return {"kind": "revision", "groups": accepted, "pending_issues": pending,
                "rejected_issues": rejections,
                "protocol_complete": obj.get("complete") is True and fw._transport_complete(response),
                "complete": obj.get("complete") is True and fw._transport_complete(response) and not pending}
    return parse


def apply_groups(original, blocks, groups):
    index = {b["block_id"]: b for b in blocks}
    edits = []
    for group in groups:
        if group["original_body_sha256"] != fw._text_hash(original):
            raise CandidateError("original_body_hash_mismatch")
        for patch in group["replacements"]:
            block = index[patch["block_id"]]
            start, end = block["start"], block["end"]
            if not 0 <= start < end <= len(original) or fw._text_hash(original[start:end]) != patch["original_sha256"]:
                raise CandidateError("original_block_range_or_hash_mismatch")
            edits.append((start, end, patch["content"]))
    edits.sort()
    if any(a[1] > b[0] for a, b in zip(edits, edits[1:])):
        raise CandidateError("overlapping_issue_groups")
    result = original
    for start, end, content in reversed(edits):
        result = result[:start] + content + result[end:]
    return result


def _cited_handles(book, text):
    aliases = book.get("source_aliases", {})
    known = book.get("source_identities", {})
    handles = []
    for bracket in re.findall(r"\[([^\[\]\n]+)\]", text):
        for value in re.split(r"[,;，；\s]+", bracket):
            canonical = aliases.get(value, value)
            if canonical in known and canonical not in handles:
                handles.append(canonical)
    return handles


def _reader_payload(pack, original, blocks):
    payload = we.evidence_payload(pack, list(pack["tasks"]), atom_ids=[])
    payload.pop("reread_source_ids", None)  # Reader does not see the source inventory.
    previous = 0
    exact_blocks = []
    for block in blocks:
        exact_blocks.append({**block, "separator_before": original[previous:block["start"]]})
        previous = block["end"]
    payload.update(original_body_sha256=fw._text_hash(original), blocks=exact_blocks,
                   trailing_separator=original[previous:], body_representation="ordered complete blocks with exact separators")
    return payload


def _editor_payload(pack, original, blocks, issue, atom_ids, source_handles):
    ids = issue["related_task_ids"]
    payload = we.select_atoms(pack, ids, atom_ids)
    targets = set(issue["target_block_ids"])
    positions = {i for i, b in enumerate(blocks) if b["block_id"] in targets}
    neighbors = {j for i in positions for j in (i-1, i+1) if 0 <= j < len(blocks)}
    payload.update(body_markdown=original, original_body_sha256=fw._text_hash(original),
        body_access="read_only_navigation", reader_issues=[issue],
        editable_blocks=[b for b in blocks if b["block_id"] in targets],
        neighbor_blocks=[blocks[i] for i in sorted(neighbors) if blocks[i]["block_id"] not in targets],
        block_navigation=[{k: v for k, v in b.items() if k != "content"} for b in blocks],
        reread_source_handles=list(source_handles),
        reread_contract="All complete archived atoms of each requested source; raw records retained in local input archive")
    return payload


def _issue_evidence(pack, book, blocks, issue):
    ids = issue["related_task_ids"]
    # Absent task references do not expand scientific input to the
    # chapter/book. Full intent remains available, evidence starts
    # from exactly cited/reader-named sources and can be reread.
    atom_ids = ([atom["atom_id"] for atom in we.evidence_payload(pack, ids)["evidence_atoms"]]
                if ids else [])
    target_text = "\n".join(b["content"] for b in blocks if b["block_id"] in issue["target_block_ids"])
    handles = list(dict.fromkeys(issue["source_handles"] + _cited_handles(book, target_text)))
    for handle in handles:
        canonical = book.get("source_aliases", {}).get(handle, handle)
        atom_ids.extend(pack["source_atom_ids"].get(canonical, []))
    atom_ids = list(dict.fromkeys(atom_ids))
    if not ids:
        canonical_handles = {book.get("source_aliases", {}).get(h, h) for h in handles}
        inferred = [tid for tid in pack["tasks"] if canonical_handles.intersection(pack["task_source_handles"][tid])]
        issue = {**issue, "related_task_ids": inferred or list(pack["tasks"]),
                 "task_scope_inferred_from_target_citations": bool(inferred),
                 "fallback_intent_only": not bool(inferred)}
    return issue, atom_ids


def run_scoped_revision(book, base_result, output_dir, config, client_factory=None,
                        run=False, retry_failed=False, counter=None):
    output = Path(output_dir).resolve()
    with _output_lock(output):
        return _run(book, base_result, output, config, client_factory, run, retry_failed, counter)


def _run(book, base_result, output, config, client_factory, run, retry_failed, counter):
    config = fw.validate_config(config)
    input_hash = _hash(book)
    validation_base = deepcopy(base_result)
    if validation_base.get("body_complete") is True:
        validation_base["complete"] = True
    base = fw._base_validate(validation_base, book, input_hash)
    original, catalog = base["body_markdown"], fullbody_task_catalog(book)
    blocks, pack = index_blocks(original, book, base), we.compile_evidence(book)
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:10]
    run_dir = output / "runs" / run_id
    paths = [Path(__file__), Path(we.__file__), PROMPT_ROOT / "scoped_reader.md", PROMPT_ROOT / "scoped_revision.md"]
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    hashes.update(fw._source_file_hashes())
    code_hash = _hash(hashes)
    input_path = output / "inputs" / input_hash / "FULL_BODY_INPUT.json"
    _write(input_path, book)
    _write_body(run_dir / "ORIGINAL_FULL_BODY.md", original)
    _write(run_dir / "ORIGINAL_FULL_BODY_RESULT.json", base_result)
    _write(run_dir / "BLOCK_INDEX.json", blocks)
    stages, groups, pending, rejections = [], [], [], []
    deps = [{"stage_id": "verified_base", "result_sha256": _hash(base_result), "body_sha256": fw._text_hash(original), "book_sha256": input_hash}]
    def execute(stage_id, role, payload, prompt, parser):
        messages = [{"role": "system", "content": (PROMPT_ROOT / prompt).read_text()},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}]
        stage = fw._stage(stage_id, role, messages, config[role], config, counter)
        outcome = fw._execute_stage(stage, output=output, run_dir=run_dir, route="scoped_revision", code_hash=code_hash,
            input_hash=input_hash, dependencies=deepcopy(deps), client_factory=client_factory, run=run,
            retry_failed=retry_failed, config=config, counter=counter, parser=parser)
        stages.append(outcome)
        _write(run_dir / "STAGES.json", stages)
        return outcome
    reader, error = {}, None
    try:
        read = execute("scoped_reader", "reader", _reader_payload(pack, original, blocks), "scoped_reader.md", reader_parser(blocks, catalog, book))
        reader = read.get("result", {})
        pending.extend(reader.get("pending_issues", []))
        if read["status"] != "complete":
            pending.append({"issue_id": "reader_stage", "status": read["status"]})
        else:
            deps.append(fw._dependency(read))
            for i, issue in enumerate(reader.get("issues", []), 1):
                issue, atom_ids = _issue_evidence(pack, book, blocks, issue)
                sources = []
                for round_ in range(config["max_rereads_per_window"] + 1):
                    if len(stages) >= config["max_author_calls"]:
                        pending.append({"issue_id": issue["issue_id"], "status": "call_limit_reached"})
                        break
                    outcome = execute(f"scoped_revision_{i:03d}_{round_:02d}", "reviser",
                        _editor_payload(pack, original, blocks, issue, atom_ids, sources), "scoped_revision.md", group_parser(original, blocks, [issue]))
                    parsed = outcome.get("result", {})
                    safe_partial = (parsed.get("kind") == "revision" and parsed.get("protocol_complete") is True
                                    and parsed.get("transport_complete") is True and not parsed.get("call_error"))
                    if outcome["status"] != "complete" and not safe_partial:
                        pending.append({"issue_id": issue["issue_id"], "status": outcome["status"]})
                        break
                    if parsed.get("kind") != "reread":
                        for group in parsed.get("groups", []):
                            try:
                                candidate = apply_groups(original, blocks, groups + [group])
                                structural = parse_fullbody_response({"body_markdown": candidate,
                                    "completed_task_ids": list(catalog), "complete": True}, book)
                                if not structural.get("complete"):
                                    raise CandidateError("scoped_group_structural_validation_failed")
                            except (CandidateError, KeyError) as exc:
                                pending.append({"issue_id": issue["issue_id"], "validation_errors": [str(exc)]})
                                fw._reject_outcome(outcome, "scoped_group_application_failed", str(exc))
                            else:
                                groups.append(group)
                        pending.extend(parsed.get("pending_issues", []))
                        rejections.extend(parsed.get("rejected_issues", []))
                        break
                    requested_atoms = parsed["read_atom_ids"]
                    requested_sources = [book.get("source_aliases", {}).get(h, h) for h in parsed["read_source_handles"]]
                    if (any(a not in pack["atoms"] for a in requested_atoms) or
                        any(h not in book.get("source_identities", {}) for h in requested_sources)):
                        pending.append({"issue_id": issue["issue_id"], "status": "unknown_reread_identity"})
                        break
                    fresh_atoms = [a for a in requested_atoms if a not in atom_ids]
                    fresh_sources = [h for h in requested_sources if h not in sources]
                    if round_ == config["max_rereads_per_window"] or not (fresh_atoms or fresh_sources):
                        pending.append({"issue_id": issue["issue_id"], "status": "reread_limit_or_no_progress"})
                        break
                    atom_ids.extend(fresh_atoms)
                    for handle in fresh_sources:
                        atom_ids.extend(pack["source_atom_ids"][handle])
                    atom_ids = list(dict.fromkeys(atom_ids))
                    sources.extend(fresh_sources)
                    deps.append(fw._dependency(outcome))
    except Exception as exc:
        error = fw._safe_error(exc)
        pending.append({"issue_id": "execution_failure", "error": error})
    accounted = {g["issue_id"] for g in groups} | {r["issue_id"] for r in rejections} | {
        p.get("issue_id") for p in pending if isinstance(p.get("issue_id"), str)}
    for issue in reader.get("issues", []):
        if isinstance(issue, dict) and issue.get("issue_id") not in accounted:
            pending.append({"issue_id": issue.get("issue_id"), "status": "not_processed"})
    body = apply_groups(original, blocks, groups)
    checked = parse_fullbody_response({"body_markdown": body, "completed_task_ids": list(catalog), "complete": True}, book)
    if not checked.get("complete"):
        pending.append({"issue_id": "structural_validation", "issues": checked.get("issues", [])})
        body, groups = original, []
    complete = reader.get("complete") is True and not pending
    calls = sum(s.get("model_calls", 0) for s in stages)
    paid = None if any(s.get("paid_dispatch_count") is None for s in stages) else sum(s.get("paid_dispatch_count", 0) for s in stages)
    result = {**deepcopy(base_result), "schema_version": SCHEMA_VERSION + ".result", "run_id": run_id,
        "input_hash": input_hash, "input_path": str(input_path), "body_markdown": body, "body_sha256": fw._text_hash(body),
        "complete": complete, "body_complete": True, "revision_complete": complete, "reader_revision_complete": complete,
        "reader_revision_status": "complete" if complete else "partial" if groups else "pending",
        "status": "complete" if complete else "preview" if not run else "pending",
        "pending_issues": pending, "pending_reader_issues": pending, "accepted_groups": groups, "accepted_patches": [],
        "rejected_issues": rejections, "reader_assessment": {k: reader.get(k) for k in ("understanding", "assessment")},
        "semantic_quality_unreviewed": True, "material_preserved": True, "selected_kind": "scoped_revision" if groups else "original_base",
        "requested_route": "scoped_revision", "effective_route": "scoped_revision", "output_dir": str(output),
        "model_calls": calls, "client_invocations": calls, "paid_dispatch_count": paid,
        "cost_summary": fw._cost_summary(stages, base_result), "source_file_hashes": hashes,
        "provenance": {"base_result_sha256": _hash(base_result), "original_body_sha256": fw._text_hash(original),
                       "block_index_path": str(run_dir / "BLOCK_INDEX.json"), "base_cost_recharged": False},
        "stage_lineage": [fw._dependency(s) | {"role": s["role"], "status": s["status"]} for s in stages],
        "stages": [{k: v for k, v in s.items() if k != "result"} for s in stages],
        "citation_diagnostics": {"before": _cited_handles(book, original), "after": _cited_handles(book, body), "quota_enforced": False}}
    if groups:
        result["segments"] = [{"segment_id": "scoped_revised_body", "body_markdown": body, "sha256": fw._text_hash(body),
            "task_ids": list(catalog), "chapter_ids": [c["chapter_id"] for c in book["chapters"]]}]
        for disposition in result.get("task_dispositions", []):
            location = disposition.get("location")
            if isinstance(location, dict) and location.get("anchor") and body.count(location["anchor"]) != 1:
                disposition["original_location"] = disposition.pop("location")
                disposition["location_invalidated_by_revision"] = True
    _write(run_dir / "FULL_BODY_RESULT.json", result)
    _write_body(run_dir / "FULL_BODY.md", body)
    selected = result
    if (output / "FULL_BODY_RESULT.json").exists():
        previous = _read(output / "FULL_BODY_RESULT.json")
        if previous.get("complete") and not complete:
            selected = previous
    if selected is result:
        _write(output / "FULL_BODY_RESULT.json", result)
        _write_body(output / "FULL_BODY.md", body)
    report = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "status": result["status"],
        "source_file_hashes": hashes, "code_hash": code_hash, "stages": result["stages"],
        "cost_summary": result["cost_summary"], "model_calls": calls, "paid_dispatch_count": paid,
        "selected_version": selected["run_id"], "verification_scope": "Exact scoped application and provenance only; assess the natural complete manuscript independently."}
    _write(run_dir / "IMPLEMENTATION_REPORT.json", report)
    _write(output / "IMPLEMENTATION_REPORT.json", report)
    return {**result, "selected_version": selected["run_id"], "selected_result_path": str(output / "FULL_BODY_RESULT.json"),
            "selected_input_matches_current": selected.get("input_hash") == input_hash}
