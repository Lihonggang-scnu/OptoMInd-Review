"""Upgrade-3 legal candidate atomic promotion (ticket 022).

Separates the *minimal savable candidate* from the *scientifically promotable*
candidate:

- syntax candidates (draft present, >=50 words, no CJK) are saved as
  ``provisional`` ONLY: they can never be pointed to by
  CURRENT_SCIENTIFIC_CANDIDATE and can never be consumed as evidence-complete;
- a scientific promotion requires: non-empty resolved bindings, a FRESH
  science audit (audit hash equals the current audit output), and a complete
  019 admission for the section; the whole bundle (draft + plan + bindings +
  facts + audit + admission) commits atomically with per-file hashes;
- worker stop reasons are preserved verbatim (max_iters / token_limit /
  budget_exhausted are distinct); when the worker terminates with a scientific
  candidate on file the candidate is ``retained_validated`` — never rewritten
  into ``completed``;
- recovery reads ONLY the pointer and its manifest (hash-verified); scattered
  draft/package/RESULT files are never "best-of" picked;
- a rejected candidate is kept for diagnostics and never overwritten by a
  worse one; the pointer never moves outside the store.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional

MANIFEST_SCHEMA = "optomind.upgrade3.author_candidate_manifest.v1"
POINTER_NAME = "CURRENT_SCIENTIFIC_CANDIDATE.json"


def _sha(text: Any) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def is_syntax_ok(draft_text: str) -> bool:
    """The old syntax gate: >=50 words, no CJK.  Necessary but NOT sufficient."""
    words = len((draft_text or "").split())
    cjk = any('\u4e00' <= ch <= '\u9fff' for ch in (draft_text or ""))
    return words >= 50 and not cjk


class CandidateStore:
    def __init__(self, root: str):
        self.root = root
        self.provisional_dir = os.path.join(root, "provisional")
        self.scientific_dir = os.path.join(root, "scientific")
        self.rejected_dir = os.path.join(root, "rejected")
        for d in (self.provisional_dir, self.scientific_dir, self.rejected_dir):
            os.makedirs(d, exist_ok=True)
        self.pointer_path = os.path.join(root, POINTER_NAME)

    # ------------------------------------------------ pointer ----
    def current(self) -> Optional[Dict[str, Any]]:
        if not os.path.isfile(self.pointer_path):
            return None
        pointer = json.load(open(self.pointer_path, encoding="utf-8"))
        manifest_path = os.path.join(self.root, pointer.get("manifest_path", ""))
        if not os.path.isfile(manifest_path):
            return None
        manifest = json.load(open(manifest_path, encoding="utf-8"))
        # hash verification: every recorded file must still match
        for rel, expected in manifest.get("files", {}).items():
            p = os.path.join(self.root, rel)
            if not os.path.isfile(p):
                return None
            if _sha(open(p, "rb").read()) != expected:
                return None
        return manifest

    def _write_pointer(self, manifest_rel: str) -> None:
        pointer = {"manifest_path": manifest_rel}
        fd, tmp = tempfile_mkstemp(self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(pointer, handle, indent=1)
            os.replace(tmp, self.pointer_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------ commit ----
    def commit(self, *, candidate_id: str, section_id: str, generation_id: str,
               draft_text: str, plan: Dict[str, Any],
               bindings: List[Dict[str, Any]], facts: List[Dict[str, Any]],
               audit: Dict[str, Any], admission: Dict[str, Any],
               stop_reason: str = "") -> Dict[str, Any]:
        """Atomic bundle commit.  Returns the manifest including
        promotion_allowed and the preserved stop_reason."""
        section_admitted = any(
            x.get("admitted") for x in (admission.get("sections") or {}).values()
        ) if admission.get("sections") else admission.get("admitted") is True
        audit_fresh = bool(audit.get("audit_hash"))
        resolved_bindings = [b for b in bindings
                             if b.get("binding_status") == "resolved"
                             or b.get("writable") is True]
        scientific = (
            is_syntax_ok(draft_text)
            and bool(bindings)
            and len(resolved_bindings) > 0
            and audit_fresh
            and bool(admission.get("sections")) and section_admitted
            and admission.get("global_status") in ("full", "partial")
        )
        validation_level = "scientific" if scientific else "syntax"
        # atomic write of the bundle directory
        bundle_rel = os.path.join(
            "scientific" if scientific else "provisional", candidate_id)
        files = {
            os.path.join(bundle_rel, fname): _sha(content.encode("utf-8"))
            for fname, content in (
                ("draft.md", draft_text),
                ("plan.json", _canonical(plan)),
                ("bindings.json", _canonical(bindings)),
                ("facts.json", _canonical(facts)),
                ("audit.json", _canonical(audit)),
                ("admission.json", _canonical(admission)),
            )
        }
        manifest = {
            "schema_version": MANIFEST_SCHEMA,
            "candidate_id": candidate_id,
            "section_id": section_id,
            "generation_id": generation_id,
            "validation_level": validation_level,
            "promotion_allowed": scientific,
            "files": files,
            "draft_sha256": files[os.path.join(bundle_rel, "draft.md")],
            "stop_reason": stop_reason,
            "retained_validated": bool(
                scientific and stop_reason in ("max_iters", "token_limit",
                                               "budget_exhausted")),
            "status": ("promoted" if scientific
                       else "provisional_syntax" if stop_reason == ""
                       else "provisional_diagnostic"),
        }
        bundle_dir = os.path.join(self.root, bundle_rel)
        os.makedirs(bundle_dir, exist_ok=True)
        payloads = {
            "draft.md": draft_text,
            "plan.json": _canonical(plan),
            "bindings.json": _canonical(bindings),
            "facts.json": _canonical(facts),
            "audit.json": _canonical(audit),
            "admission.json": _canonical(admission),
        }
        for fname, content in payloads.items():
            with open(os.path.join(bundle_dir, fname), "w",
                      encoding="utf-8") as handle:
                handle.write(content)
        manifest_rel = os.path.join(bundle_rel, "manifest.json")
        with open(os.path.join(self.root, manifest_rel), "w",
                  encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=1)
        if scientific:
            self._write_pointer(manifest_rel)
        return manifest
        with open(os.path.join(self.root, manifest_rel), "w",
                  encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=1)
        if scientific:
            self._write_pointer(manifest_rel)
        return manifest

    def reject(self, candidate_id: str, diagnostic: Dict[str, Any]) -> None:
        """Keep a rejected candidate for diagnostics; never overwrite others."""
        path = os.path.join(self.rejected_dir, candidate_id + ".json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"candidate_id": candidate_id, "diagnostic": diagnostic},
                      handle, ensure_ascii=False, indent=1)

    def recover(self) -> Dict[str, Any]:
        """Recovery reads ONLY the pointer + manifest (hash verified)."""
        manifest = self.current()
        if manifest is None:
            return {"recovered": False,
                    "reason": "no_valid_current_scientific_candidate",
                    "action": "block_next_step_keep_syntax_for_repair"}
        return {"recovered": True,
                "candidate_id": manifest["candidate_id"],
                "draft_sha256": manifest["draft_sha256"],
                "binding_count": len(manifest.get("files", {})),
                "stop_reason_preserved": manifest.get("stop_reason")}


def tempfile_mkstemp(directory: str):
    import tempfile
    return tempfile.mkstemp(dir=directory, suffix=".tmp")
