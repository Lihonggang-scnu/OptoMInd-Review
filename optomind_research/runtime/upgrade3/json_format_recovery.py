"""Bounded, syntax-only recovery for maker JSON; never complete missing content.

The existing json-repair library is checked against a mechanical candidate using
the existing quote helper, raw string controls and trailing commas. It never
inserts structural delimiters, keys or values. The shared permissive writer parsers are deliberately untouched.
"""
from __future__ import annotations

import hashlib
from importlib.metadata import version
import json
import math
import re
from typing import Any

from json_repair import repair_json

from .chapter_arrangement import _escape_inner_json_quotes

MAX_RECOVERY_CHARS = 2_000_000
MAX_RECOVERY_EDITS = 128
_FENCE = re.compile(r"\A[ \t\r\n]*```(?:json)?[ \t]*\r?\n(?P<body>.*?)\r?\n```[ \t\r\n]*\Z", re.S)


class JsonFormatError(ValueError):
    """Malformed, ambiguous or out-of-policy JSON."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JsonFormatError("duplicate_json_key")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise JsonFormatError("nonfinite_json_number")


def _float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise JsonFormatError("nonfinite_json_number")
    return parsed


def strict_json_loads(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float)
    except RecursionError as exc:
        raise JsonFormatError("json_recovery_depth_limit") from exc


def _mechanical_candidate(text: str, base: int, edits: list[dict[str, Any]]) -> str:
    """Reuse quote repair; allow only controls/trailing commas beyond its edits."""
    quoted = _escape_inner_json_quotes(text)
    # The existing helper only inserts a backslash before an original quote.
    # Track original offsets and independently enforce that exact contract.
    positions, quote_edits = [], set()
    i = j = 0
    while j < len(quoted):
        if i < len(text) and quoted[j] == text[i]:
            positions.append(i)
            i, j = i + 1, j + 1
        elif i < len(text) and text[i] == '"' and quoted[j:j + 2] == '\\"':
            quote_edits.add(j)
            positions.append(i)
            edits.append(dict(operation="escape_inner_quote", original_offset=base + i,
                              original='"', replacement='\\"'))
            j += 1
        else:
            raise JsonFormatError("json_recovery_nonmechanical_quote_edit")
    if i != len(text):
        raise JsonFormatError("json_recovery_content_loss")
    def following(index: int) -> str:
        index += 1
        while index < len(quoted) and quoted[index] in " \t\r\n":
            index += 1
        return quoted[index:index + 1]

    out: list[str] = []
    in_string = escaped = False
    count = 0
    for j, char in enumerate(quoted):
        if j in quote_edits:
            count += 1
        if escaped:
            out.append(char)
            escaped = False
            continue
        if in_string and char == "\\":
            escaped = True
        elif char == '"':
            if in_string:
                if count % 2 or (count and following(j) == ":"):
                    raise JsonFormatError("json_recovery_ambiguous_quote")
                count = 0
            in_string = not in_string
        elif in_string and ord(char) < 0x20:
            replacement = json.dumps(char)[1:-1]
            edits.append(dict(operation="escape_string_control", original_offset=base + positions[j],
                              original=char, replacement=replacement))
            out.append(replacement)
            continue
        elif not in_string and char == "," and following(j) in ("}", "]"):
            edits.append(dict(operation="remove_trailing_comma", original_offset=base + positions[j],
                              original=",", replacement=""))
            continue
        out.append(char)
    if in_string or escaped:
        raise JsonFormatError("json_recovery_incomplete_string")
    if len(edits) > MAX_RECOVERY_EDITS:
        raise JsonFormatError("json_recovery_edit_limit")
    return "".join(out)


def recover_json_format(text: str) -> tuple[Any, dict[str, Any] | None]:
    """Strict parse first; return an auditable syntax-only repair if possible.

    Offsets are Unicode character offsets in the immutable original string;
    hashes cover UTF-8 text. No network/model calls or schema-dependent repairs.
    """
    try:
        return strict_json_loads(text), None
    except json.JSONDecodeError:
        pass
    if len(text) > MAX_RECOVERY_CHARS:
        raise JsonFormatError("json_recovery_size_limit")
    original = text
    edits: list[dict[str, Any]] = []
    base = 0
    if text.startswith("\ufeff"):
        edits.append(dict(operation="remove_bom", original_offset=0, original="\ufeff", replacement=""))
        text, base = text[1:], 1
    fence = _FENCE.fullmatch(text)
    if fence:
        start, end = fence.span("body")
        edits.extend([
            dict(operation="remove_outer_fence", original_offset=base, original=text[:start], replacement=""),
            dict(operation="remove_outer_fence", original_offset=base + end, original=text[end:], replacement=""),
        ])
        text, base = fence.group("body"), base + start
    normalized = _mechanical_candidate(text, base, edits)
    value = strict_json_loads(normalized)
    # No schema/salvage options: the library proposes syntax recovery only.
    # Its result is never trusted to fill gaps. A strict-parsed mechanical
    # candidate must independently exist and agree, including all content.
    try:
        candidate = repair_json(text, return_objects=True, strict=True)
    except (ValueError, RecursionError) as exc:
        raise JsonFormatError("json_recovery_library_rejected") from exc
    if candidate != value:
        raise JsonFormatError("json_recovery_candidate_disagreement")
    if not edits:
        raise JsonFormatError("json_recovery_no_supported_edit")
    audit = {
        "method": "json_repair_crosschecked_v1",
        "library_version": version("json-repair"),
        "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "normalized_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "edits": sorted(edits, key=lambda edit: edit["original_offset"]),
        "normalized_text": normalized,
    }
    return value, audit
