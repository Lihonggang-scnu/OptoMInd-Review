"""Fail-closed snapshot and evidence contracts for downstream BODY revisions.

Offsets are Python string offsets, never offsets in a normalized copy. UTF-8
round trips preserve all untouched bytes, including CRLF. These deterministic
checks enforce provenance and numeric/citation editorial boundaries;
they do not prove that a scientific assertion is entailed by its evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .review_unit_writer import citations_in


class RevisionContractError(ValueError):
    """An input, evidence reference, or snapshot-bound patch is unsafe."""


def sha256_text(text: str) -> str:
    if not isinstance(text, str):
        raise RevisionContractError("hash_input_not_text")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fail(reason: str) -> None:
    raise RevisionContractError(reason)


def _text(value: Any, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        _fail(f"invalid_{field}")
    return value


def _blocks(text: str) -> list[dict[str, Any]]:
    """Paragraph snapshots; blank lines inside fenced code stay in that block."""
    spans: list[tuple[int, int]] = []
    start = offset = 0
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
        end = offset + len(line)
        if not line.strip() and fence is None:
            if text[start:offset].strip():
                spans.append((start, offset))
            start = end
        offset = end
    if text[start:].strip():
        spans.append((start, len(text)))
    return [{"block_id": f"B{i:04d}", "start": a, "end": b,
             "text": text[a:b], "sha256": sha256_text(text[a:b])}
            for i, (a, b) in enumerate(spans, 1)]


def normalize_case(mapping: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(mapping, Mapping):
        _fail("case_not_object")
    case = dict(mapping)
    draft = _text(case.get("draft_text"), "draft_text")
    digest = sha256_text(draft)
    if case.get("base_sha256") not in (None, digest):
        _fail("stale_base_sha256")
    blocks = _blocks(draft)
    if case.get("blocks") is not None and case["blocks"] != blocks:
        _fail("snapshot_blocks_mismatch")
    raw = case.get("materials", {})
    if isinstance(raw, list):
        converted = {}
        for record in raw:
            if not isinstance(record, Mapping):
                _fail("invalid_material_record")
            key = record.get("material_id", record.get("id"))
            _text(key, "material_id")
            if key in converted:
                _fail(f"duplicate_material_id:{key}")
            converted[key] = record
        raw = converted
    if not isinstance(raw, Mapping):
        _fail("materials_not_object")
    materials = {}
    for key, record in raw.items():
        _text(key, "material_id")
        if isinstance(record, str):
            record = {"text": record}
        if not isinstance(record, Mapping):
            _fail(f"invalid_material:{key}")
        item = dict(record)
        item["text"] = _text(item.get("text", ""), "material_text", empty=True)
        item.setdefault("title", "")
        item.setdefault("summary", "")
        handles = item.get("source_handles", [])
        if not isinstance(handles, list) or any(not isinstance(h, str) for h in handles):
            _fail(f"invalid_source_handles:{key}")
        item["source_handles"] = handles
        materials[key] = item
    identity = case.get("source_identity_map", {})
    if not isinstance(identity, Mapping):
        _fail("source_identity_map_not_object")
    case.update(case_id=str(case.get("case_id", "case")), draft_text=draft,
                base_sha256=digest, blocks=blocks, materials=materials,
                source_identity_map=dict(identity))
    for field in ("research_question", "scope", "outline"):
        case.setdefault(field, "")
    return case


def load_case(path: str | Path) -> dict[str, Any]:
    """Read JSON and case-relative UTF-8 files without newline translation."""
    path = Path(path)
    try:
        raw = json.loads(path.read_bytes().decode("utf-8"))
        if not isinstance(raw, dict):
            _fail("case_not_object")
        def read(relative: Any) -> str:
            return (path.parent / _text(relative, "file_path")).read_bytes().decode("utf-8")
        if "draft_text" not in raw:
            raw["draft_text"] = read(raw.get("draft_file", raw.get("draft_path")))
        if "materials_file" in raw:
            if "materials" in raw:
                _fail("ambiguous_materials_input")
            raw["materials"] = json.loads(read(raw["materials_file"]))
        materials = raw.get("materials", {})
        records = materials.values() if isinstance(materials, dict) else materials
        for record in records:
            if isinstance(record, dict) and "text" not in record:
                name = record.get("text_file", record.get("path"))
                if name is not None:
                    record["text"] = read(name)
        return normalize_case(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise RevisionContractError(f"case_load_failed:{exc}") from exc


def resolve_target(case: Mapping[str, Any], issue: Mapping[str, Any]) -> dict[str, Any]:
    case = normalize_case(case)
    if not isinstance(issue, Mapping):
        _fail("issue_not_object")
    block_id = _text(issue.get("target_block_id"), "target_block_id")
    found = [b for b in case["blocks"] if b["block_id"] == block_id]
    if len(found) != 1:
        _fail(f"unknown_target_block:{block_id}")
    block = found[0]
    original = _text(issue.get("original_text"), "original_text")
    # Find overlapping occurrences as well (str.count is insufficient).
    positions = [m.start() for m in re.finditer(f"(?={re.escape(original)})", block["text"])]
    if len(positions) != 1:
        _fail(f"target_not_unique_in_block:{block_id}:{len(positions)}")
    start = block["start"] + positions[0]
    return {"block_id": block_id, "start": start, "end": start + len(original),
            "original_text": original, "sha256": sha256_text(original),
            "block_sha256": block["sha256"]}


def evidence_for_issue(case: Mapping[str, Any], issue: Mapping[str, Any]) -> dict[str, Any]:
    case = normalize_case(case)
    if not isinstance(issue, Mapping):
        _fail("issue_not_object")
    kind = issue.get("kind")
    if not isinstance(kind, str) or kind not in {"editorial", "scientific", "missing"}:
        _fail("unknown_issue_kind")
    ids = issue.get("evidence_ids", [])
    if not isinstance(ids, list) or any(not isinstance(k, str) for k in ids):
        _fail("invalid_evidence_ids")
    if len(ids) != len(set(ids)):
        _fail("duplicate_evidence_id")
    selected = {}
    for key in ids:
        if key not in case["materials"]:
            _fail(f"unknown_evidence_id:{key}")
        item = case["materials"][key]
        if not item["text"].strip():
            _fail(f"empty_evidence_material:{key}")
        selected[key] = dict(item)
    if kind in {"scientific", "missing"} and not selected:
        _fail("scientific_change_requires_material")
    return selected


_CITATION = re.compile(r"(?<![A-Za-z0-9_])P\d{3,}(?![A-Za-z0-9_])")
# Do not treat digits embedded in identifiers such as P9999 as quantities.
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")


def _supported_handles(case: Mapping[str, Any], evidence: Mapping[str, Any]) -> set[str]:
    handles: set[str] = set()
    for key, material in evidence.items():
        handles.update(h for h in material["source_handles"] if _CITATION.fullmatch(h))
        if _CITATION.fullmatch(key):
            handles.add(key)
    # Exact IDs only: titles, authors and fuzzy paper names are never matched.
    for handle, identity in case["source_identity_map"].items():
        if not isinstance(handle, str) or not _CITATION.fullmatch(handle):
            continue
        if isinstance(identity, str):
            ids = [identity]
        elif isinstance(identity, Mapping):
            ids = identity.get("material_ids", [identity.get("material_id")])
        else:
            ids = []
        if isinstance(ids, list) and any(k in evidence for k in ids if isinstance(k, str)):
            handles.add(handle)
    return handles


def validate_patch(case: Mapping[str, Any], issue: Mapping[str, Any],
                   proposal: Mapping[str, Any]) -> dict[str, Any]:
    case = normalize_case(case)
    if not isinstance(issue, Mapping) or not isinstance(proposal, Mapping):
        _fail("issue_or_proposal_not_object")
    issue_id = _text(issue.get("issue_id"), "issue_id")
    target = resolve_target(case, issue)
    evidence = evidence_for_issue(case, issue)
    if "evidence_ids" in proposal:
        ids = proposal["evidence_ids"]
        if not isinstance(ids, list) or any(not isinstance(key, str) for key in ids):
            _fail("invalid_proposal_evidence_ids")
        if len(ids) != len(set(ids)):
            _fail("duplicate_proposal_evidence_id")
        if any(key not in evidence for key in ids):
            _fail("proposal_evidence_not_selected")
        evidence = {key: evidence[key] for key in ids}
        if issue["kind"] in {"scientific", "missing"} and not evidence:
            _fail("scientific_change_requires_material")
    operation = issue.get("operation", "replace")
    if not isinstance(operation, str) or operation not in {"replace", "remove", "insert_after"}:
        _fail("unknown_operation")
    bound = {"base_sha256": case["base_sha256"], "issue_id": issue_id,
             "target_block_id": target["block_id"], "operation": operation,
             "original_text": target["original_text"], "original_sha256": target["sha256"],
             "block_sha256": target["block_sha256"], "start": target["start"], "end": target["end"]}
    for key, expected in bound.items():
        if key in proposal and proposal[key] != expected:
            _fail(f"patch_binding_mismatch:{key}")
    replacement = _text(proposal.get("replacement_text", ""), "replacement_text", empty=True)
    if operation == "remove" and replacement:
        _fail("remove_has_replacement")
    if operation != "remove" and not replacement.strip():
        _fail("empty_replacement")
    original = target["original_text"]
    if operation == "replace" and replacement == original:
        _fail("no_op_patch")
    final = original + replacement if operation == "insert_after" else replacement
    preserve = issue.get("preserve", [])
    if not isinstance(preserve, list) or any(not isinstance(s, str) or not s for s in preserve):
        _fail("invalid_preserve")
    if any(s not in original or s not in final for s in preserve):
        _fail("preserve_contract_violated")
    introduced = set(citations_in(replacement)) - set(citations_in(original))
    if introduced - _supported_handles(case, evidence):
        _fail("unsupported_introduced_citation:" + ",".join(sorted(introduced - _supported_handles(case, evidence))))
    if issue["kind"] == "editorial":
        if introduced:
            _fail("editorial_introduces_citation")
        if set(_NUMBER.findall(replacement)) - set(_NUMBER.findall(original)):
            _fail("editorial_introduces_number")
    return {**bound, "replacement_text": replacement, "kind": issue["kind"],
            "evidence_ids": list(evidence), "preserve": list(preserve)}


def apply_patches(case: Mapping[str, Any], patches: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Validate an entire batch before applying any patch; conflicts abort it.

    No mutable state is kept. Reusing a patch on a changed draft fails its base
    digest; running the same immutable input twice is deterministic, not a replay
    against a changed manuscript. No baseline or output files are modified here.
    """
    case = normalize_case(case)
    if isinstance(patches, (str, bytes)) or not isinstance(patches, Sequence):
        _fail("patches_not_sequence")
    checked = []
    seen = set()
    for patch in patches:
        if not isinstance(patch, Mapping):
            _fail("patch_not_object")
        for required in ("base_sha256", "block_sha256", "original_sha256", "start", "end"):
            if required not in patch:
                _fail(f"unbound_patch:{required}")
        item = validate_patch(case, patch, patch)
        if item["issue_id"] in seen:
            _fail("duplicate_patch_issue")
        seen.add(item["issue_id"])
        checked.append(item)
    ordered = sorted(checked, key=lambda p: (p["start"], p["end"]))
    for left, right in zip(ordered, ordered[1:]):
        # Protect whole anchor ranges even for insertions, avoiding ambiguous
        # insert/delete combinations and same-anchor insert ordering.
        if right["start"] < left["end"]:
            _fail("overlapping_patches")
    text = case["draft_text"]
    for patch in reversed(ordered):
        start = patch["end"] if patch["operation"] == "insert_after" else patch["start"]
        text = text[:start] + patch["replacement_text"] + text[patch["end"]:]
    return {"draft_text": text, "applied": checked, "skipped": []}
