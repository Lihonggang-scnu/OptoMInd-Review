"""Upgrade-3 prompt & tool-call invocation identity (ticket 008).

Minimal identity layer at the real call boundary — deliberately NOT a prompt
platform: no registry, no service, no multi-layer compat.

- Call-scoped manifests: the manifest directory is passed in by the caller per
  invocation; there is no globally bound sink, so an S01 manifest can never be
  silently reused as the manifest for S02 (the donor's singleton defect).
- Every compiled call records: prompt_source_hash, compiled_hash, schema_hash,
  context_hash, actual_model (filled after the response), section_id, attempt
  and the cost receipt link.
- Tool input schemas are reflection-checked against the real Python handler
  signature once, offline; unknown handles, missing required fields and path
  traversal are hard errors; model-supplied permissions/IDs are normalised
  through a known-alias table and never trusted.
- Context truncation keeps every line that carries condition markers: a
  trimmed context may lose prose, never necessary conditions.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

MANIFEST_SCHEMA = "optomind.upgrade3.call_manifest.v1"

CONDITION_MARKERS = re.compile(
    r"(?i)(condition|under \d|at \d+\s*(?:°c|k\b|um|µm|nm|hz|w\b|v\b)|temperature|pressure|"
    r"wavelength|dataset|baseline|out-of-sample|boundary|unit|per\s|budget|split|top-?k)")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class ToolContractError(Exception):
    pass


# ------------------------------------------------------------ tool schema ----
_JSON_TYPES = {"string": str, "integer": int, "number": (int, float),
               "boolean": bool, "array": list, "object": dict}


def validate_tool_schema_against_signature(schema: Dict[str, Any],
                                           handler: Callable) -> List[str]:
    """Offline reflection check: schema properties must match the real handler
    signature (names + requiredness); types must be compatible."""
    props = set((schema.get("properties") or {}).keys())
    required = set(schema.get("required") or [])
    unknown_required = required - props
    if unknown_required:
        return ["schema.required_lists_unknown_properties:%s" % sorted(unknown_required)]
    import inspect
    sig = inspect.signature(handler)
    params = {n: p for n, p in sig.parameters.items()
              if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    errors: List[str] = []
    for name in required:
        if name not in params:
            errors.append("schema.required_missing_in_handler:%s" % name)
        elif params[name].default is not inspect.Parameter.empty:
            errors.append("schema.required_but_handler_has_default:%s" % name)
    for name in props:
        declared = schema["properties"][name].get("type")
        if name not in params:
            errors.append("schema.property_missing_in_handler:%s" % name)
    for name, param in params.items():
        if param.annotation is inspect.Parameter.empty:
            continue
        ann = param.annotation
        declared = (schema.get("properties") or {}).get(name, {}).get("type")
        if declared in _JSON_TYPES:
            py = _JSON_TYPES[declared]
            origin = getattr(ann, "__origin__", None)
            if origin is not None:  # e.g. List[str]
                if declared == "array" and origin not in (list, tuple):
                    errors.append("schema.type_conflict:%s" % name)
            elif isinstance(py, tuple):
                if not (ann in py or ann is Any):
                    errors.append("schema.type_conflict:%s" % name)
            elif ann is not py and not (declared == "integer" and ann is int):
                errors.append("schema.type_conflict:%s" % name)
    return errors


# ------------------------------------------------------- id normalisation ----
def normalize_model_ids(payload: Dict[str, Any], known_aliases: Dict[str, str],
                        id_fields: Tuple[str, ...] = ("paper_id", "chunk_id",
                                                     "claim_id", "section_id",
                                                     "handle")) -> Dict[str, Any]:
    """Single-pass local alias normalisation.  Unknown handles are hard errors;
    permissions in the payload are never trusted (stripped)."""
    out: Dict[str, Any] = {}
    for key, value in payload.items():
        if key in ("permission", "writing_permission", "grants_direct"):
            continue  # model-supplied permissions are never accepted
        if key in id_fields and isinstance(value, str):
            canonical = known_aliases.get(value, known_aliases.get(value.strip(), value))
            if canonical not in known_aliases.values():
                if value in known_aliases.values():
                    canonical = value
                else:
                    raise ToolContractError("unknown_handle:%s=%s" % (key, value))
            out[key] = canonical
        elif isinstance(value, dict):
            out[key] = normalize_model_ids(value, known_aliases, id_fields)
        elif isinstance(value, list):
            out[key] = [normalize_model_ids(v, known_aliases, id_fields)
                        if isinstance(v, dict) else v for v in value]
        else:
            out[key] = value
    return out


# ---------------------------------------------------- context conditioning ----
def truncate_keep_conditions(text: str, max_chars: int) -> Tuple[str, Dict[str, Any]]:
    """Trim context for token budget while keeping every line that carries a
    condition marker.  Returns (trimmed, info)."""
    if len(text) <= max_chars:
        return text, {"truncated": False, "kept_condition_lines": 0}
    lines = text.splitlines(keepends=True)
    condition_lines = [i for i, ln in enumerate(lines) if CONDITION_MARKERS.search(ln)]
    budget = max_chars
    kept: List[int] = []
    for i, ln in enumerate(lines):
        if i in set(condition_lines):
            kept.append(i)
            budget -= len(ln)
    for i, ln in enumerate(lines):
        if budget <= 0:
            break
        if i in set(condition_lines):
            continue
        if len(ln) <= budget:
            kept.append(i)
            budget -= len(ln)
    kept_sorted = sorted(kept)
    trimmed = "".join(lines[i] for i in kept_sorted)
    info = {"truncated": True, "dropped_chars": len(text) - len(trimmed),
            "kept_condition_lines": len(condition_lines),
            "all_condition_lines_kept": True}
    return trimmed, info


# --------------------------------------------------------- call manifests ----
class InvocationContext:
    """One compiled provider call: identity + call-scoped manifest sink."""

    def __init__(self, *, task_id: str, generation_id: str, attempt_id: str,
                 section_id: str, stage: str, prompt_source_path: str,
                 manifest_dir: str, policy_hash: str):
        if not os.path.isfile(prompt_source_path):
            raise ToolContractError("prompt_source_missing:%s" % prompt_source_path)
        manifest_dir = os.path.realpath(manifest_dir)
        root = os.path.realpath(os.getcwd())
        self.task_id = task_id
        self.generation_id = generation_id
        self.attempt_id = attempt_id
        self.section_id = section_id
        self.stage = stage
        self.prompt_source_path = prompt_source_path
        self.prompt_source_hash = _sha(open(prompt_source_path, "rb").read().decode("utf-8", "replace"))
        self.manifest_dir = manifest_dir
        self.policy_hash = policy_hash

    def compile(self, *, system_prompt: str, user_payload: Dict[str, Any],
                schema: Any) -> Dict[str, Any]:
        manifest = {
            "schema_version": MANIFEST_SCHEMA,
            "task_id": self.task_id,
            "generation_id": self.generation_id,
            "attempt_id": self.attempt_id,
            "section_id": self.section_id,
            "stage": self.stage,
            "prompt_source_path": self.prompt_source_path,
            "prompt_source_hash": self.prompt_source_hash,
            "compiled_hash": _sha(_canonical({"system": system_prompt,
                                              "user": user_payload})),
            "schema_hash": _sha(_canonical(schema)),
            "context_hash": _sha(_canonical(user_payload.get("context") or {})),
            "policy_hash": self.policy_hash,
            "actual_model": None,       # filled by the transport after response
            "cost_receipt_link": None,  # filled after settlement
        }
        # call-scoped sink: the caller-provided directory, section-scoped name
        safe = re.fullmatch(r"[A-Za-z0-9_.-]+", self.section_id + "_" + self.stage)
        if not safe:
            raise ToolContractError("unsafe_manifest_name")
        path = os.path.join(self.manifest_dir,
                            "%s_%s.manifest.json" % (self.section_id, self.stage))
        os.makedirs(self.manifest_dir, exist_ok=True)
        fd, tmp = tempfile_mkstemp(self.manifest_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        manifest["manifest_path"] = path
        return manifest


def tempfile_mkstemp(directory: str):
    import tempfile
    return tempfile.mkstemp(dir=directory, suffix=".manifest.tmp")


def fill_actual_model(manifest_path: str, actual_model: str,
                      cost_receipt_link: str) -> Dict[str, Any]:
    manifest = json.load(open(manifest_path, encoding="utf-8"))
    manifest["actual_model"] = actual_model
    manifest["cost_receipt_link"] = cost_receipt_link
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=1, sort_keys=True)
    return manifest


MANIFEST_STAGE_COVERAGE = [
    # documented coverage of call boundaries that must compile manifests
    {"stage": "query_planner", "prompt": "prompts/Query Planner.txt"},
    {"stage": "section_coverage", "prompt": "per-stage prompt file"},
    {"stage": "phase3", "prompt": "per-stage prompt file"},
    {"stage": "author", "prompt": "prompts/Section Author.txt"},
    {"stage": "enhancer", "prompt": "chapter asset prompt files"},
    {"stage": "review", "prompt": "review prompt files"},
    {"stage": "frontmatter", "prompt": "abstract/intro/conclusion prompt files"},
    {"stage": "translation", "prompt": "translation prompt files"},
]
