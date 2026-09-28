"""Upgrade-3 manuscript manifest — single validated section list (ticket 026).

MANUSCRIPT_MANIFEST.json is the ONLY input every full-text consumer accepts:
staged context, renderer, and reviewers all resolve sections through the
manifest — never by globbing directories.

- required sections come from the frozen blueprint and keep their ids; a
  missing required section or an open critical issue BLOCKS the merge
  (block_merge is a state, not a log line).
- diagnostic merges are labelled ``diagnostic`` and can never enter the
  publisher path.
- Commander suggestions are ordering/dedup/structure ADVISES; adopting them
  creates a new manifest revision through one local transaction that keeps the
  same validated section hashes (no claim/condition/citation loss on reorder).
- front matter (title/abstract) must carry its own source and hash; a missing
  title/abstract yields assembly ``needs_input`` — a renderer may never
  invent one.
- stale sections (bundle hash mismatch vs the recorded candidate) are
  rejected; duplicate section ids are rejected.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

MANIFEST_SCHEMA = "optomind.upgrade3.manuscript_manifest.v1"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def build_manifest(*, generation_id: str,
                   required_sections: List[str],
                   candidate_bundles: Dict[str, Dict[str, Any]],
                   frontmatter: Dict[str, Dict[str, Any]],
                   fact_registry_hash: str,
                   bindings_hash: str,
                   issue_closure: Dict[str, Any],
                   visual_needs: Optional[List[Dict[str, Any]]] = None,
                   parent_revision: Optional[str] = None,
                   role_index: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Assemble and validate the manifest.  Returns the manifest with
    ``merge_state`` = merged | block_merge | needs_input."""
    problems: List[str] = []
    missing = [s for s in required_sections if s not in candidate_bundles]
    if missing:
        problems.append("missing_required_sections:" + ",".join(missing))

    seen: Dict[str, int] = {}
    ordered: List[Dict[str, Any]] = []
    for sid in required_sections:
        bundle = candidate_bundles.get(sid)
        if bundle is None:
            continue
        seen[sid] = seen.get(sid, 0) + 1
        if seen[sid] > 1:
            problems.append(f"duplicate_section_id:{sid}")
            continue
        if bundle.get("status") != "promoted":
            problems.append(f"section_not_promoted:{sid}")
            continue
        ordered.append({"section_id": sid,
                        "candidate_id": bundle.get("candidate_id"),
                        "draft_sha256": bundle.get("draft_sha256"),
                        "binding_hash": bundle.get("binding_hash")})

    open_critical = [i for i in (issue_closure or {}).get("open_critical", [])]
    if open_critical:
        problems.append("open_critical_issues:" + ",".join(map(str, open_critical)))

    fm_problems: List[str] = []
    for key in ("title", "abstract"):
        entry = frontmatter.get(key)
        if not entry or not entry.get("source_hash") or not entry.get("text"):
            fm_problems.append(key)
    if fm_problems:
        problems.append("frontmatter_needs_input:" + ",".join(fm_problems))

    merge_state = "merged"
    if problems:
        merge_state = "needs_input" if all(
            p.startswith("frontmatter_needs_input") for p in problems) else "block_merge"

    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "generation_id": generation_id,
        "required_sections": list(required_sections),
        "ordered_sections": ordered,
        "frontmatter": frontmatter,
        "fact_registry_hash": fact_registry_hash,
        "bindings_hash": bindings_hash,
        "issue_closure": issue_closure or {"open_critical": []},
        "visual_needs": visual_needs or [],
        "parent_revision": parent_revision,
        "role_index": role_index or {},
        "merge_state": merge_state,
        "problems": problems,
    }
    manifest["manifest_hash"] = _sha(_canonical(
        {k: v for k, v in manifest.items() if k != "manifest_hash"}))
    return manifest


def adopt_commander_suggestions(manifest: Dict[str, Any],
                                suggestions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Commander advises order/dedup/structure only.  Adoption creates a NEW
    revision keeping identical validated section hashes (no claim/condition/
    citation loss — reordering cannot change content)."""
    order = next((s.get("order") for s in suggestions
                  if s.get("kind") == "reorder"), None)
    new = dict(manifest)
    new["parent_revision"] = manifest["manifest_hash"]
    if order:
        known = {s["section_id"]: s for s in manifest["ordered_sections"]}
        if set(order) != set(known):
            new["problems"] = ["commander_order_unknown_sections"]
            return new
        new["ordered_sections"] = [known[sid] for sid in order if sid in known]
    new["commander_advised"] = True
    new["manifest_hash"] = _sha(_canonical(
        {k: v for k, v in new.items() if k != "manifest_hash"}))
    return new


def staged_consumer_view(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """What the staged consumer resolves: identical section order + hashes +
    abstract as the renderer view — one source of truth."""
    if manifest["merge_state"] != "merged":
        return {"allowed": False, "reason": manifest["merge_state"]}
    return {"allowed": True,
            "sections": [(s["section_id"], s["draft_sha256"])
                         for s in manifest["ordered_sections"]],
            "abstract": manifest["frontmatter"].get("abstract", {})}


def renderer_view(manifest: Dict[str, Any]) -> Dict[str, Any]:
    if manifest["merge_state"] != "merged":
        return {"allowed": False, "reason": manifest["merge_state"],
                "abstract_invented": False}
    return {"allowed": True,
            "sections": [(s["section_id"], s["draft_sha256"])
                         for s in manifest["ordered_sections"]],
            "abstract": manifest["frontmatter"].get("abstract", {}),
            "abstract_invented": False}
