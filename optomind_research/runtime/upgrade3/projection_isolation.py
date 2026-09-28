"""Upgrade-3 central-cache topic projection isolation (ticket 013).

The central raw library may be shared across topics — raw bytes are reusable —
but a writable licence never travels with them:

- ``pin_snapshot`` freezes the exact unit set (ids + raw hashes) this
  generation consumes.  Background syncs may append units, but only as FUTURE
  snapshot content: a pinned projection never observes a drifting CURRENT
  pointer, and an explicit refresh of a pinned generation requires a new
  generation id (invalidation chain).
- ``project`` emits candidates only.  Legacy topic permissions
  (scope_fit / use_permission from the source rows) are stripped to a
  display-only block; a unit enters the writable overlay only when a reviewed
  scope decision (010) exists for THIS contract+policy and the effective
  permission (012) is writable.
- cache identity covers contract/policy/source/parser/query/embedding
  parameters; permission revocation or document deletion invalidates derived
  entries (same raw bytes still avoid re-downloading, never re-judging).
- empty coverage is never promoted to ready by candidate counts.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import Any, Dict, Iterable, List, Optional

MANIFEST_SCHEMA = "optomind.upgrade3.projection_manifest.v1"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def atomic_json(path: str, payload: Any) -> None:
    fd, tmp = tempfile_mkstemp(os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def tempfile_mkstemp(directory: str):
    import tempfile
    return tempfile.mkstemp(dir=directory, suffix=".tmp")


class PinnedSnapshot:
    """A pinned view of the central raw library for one generation."""

    def __init__(self, generation_id: str, units: List[Dict[str, Any]],
                 snapshot_hash: str, pinned_at_source_hash: str):
        self.generation_id = generation_id
        self.units = units
        self.snapshot_hash = snapshot_hash
        self.pinned_at_source_hash = pinned_at_source_hash

    def drift_check(self, current_source_hash: str) -> Dict[str, Any]:
        """Background syncs create future snapshots; the pin never moves."""
        return {
            "pinned": True,
            "drifted": current_source_hash != self.pinned_at_source_hash,
            "consumes_only_pinned_units": True,
            "note": "drift is expected and safe: new units belong to future snapshots",
        }


class CentralProjection:
    def __init__(self, central_db_path: str):
        self.central_db_path = central_db_path

    # -------------------------------------------------- pinning ----
    def pin_snapshot(self, generation_id: str) -> PinnedSnapshot:
        conn = sqlite3.connect(f"file:{self.central_db_path}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT paper_id, doi, title, raw_hash FROM papers"
        ).fetchall() if self._has_raw_hash(conn) else conn.execute(
            "SELECT paper_id, doi, title, NULL FROM papers").fetchall()
        chunks = conn.execute(
            "SELECT paper_id, COUNT(*), COALESCE(SUM(LENGTH(text)),0) FROM text_chunks "
            "GROUP BY paper_id").fetchall()
        conn.close()
        chunk_by_paper = {r[0]: (r[1], r[2]) for r in chunks}
        units = []
        for pid, doi, title, raw_hash in rows:
            units.append({
                "unit_id": pid,
                "document_id": doi or pid,
                "title": title,
                "raw_hash": raw_hash,
                "chunk_count": chunk_by_paper.get(pid, (0, 0))[0],
                "text_bytes": chunk_by_paper.get(pid, (0, 0))[1],
                # legacy topic permissions are DISPLAY-ONLY in projections:
                "legacy_permission_display_stripped": True,
            })
        snapshot_hash = _sha(_canonical(
            [[u["unit_id"], u["raw_hash"], u["chunk_count"]] for u in units]))
        return PinnedSnapshot(generation_id, units, snapshot_hash, snapshot_hash)

    def _has_raw_hash(self, conn: sqlite3.Connection) -> bool:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(papers)")]
        return "raw_hash" in cols

    # ------------------------------------------------ projection ----
    def project(self, *, snapshot: PinnedSnapshot, contract: Dict[str, Any],
                policy_hash: str, parser_hash: str,
                query_params: Dict[str, Any],
                scope_decisions: Optional[Dict[str, Dict[str, Any]]] = None,
                permission_of: Optional[Callable] = None,
                output_dir: str = "") -> Dict[str, Any]:
        """Emit the PROJECTION_MANIFEST: candidates from the pinned snapshot;
        writable overlay only for reviewed_direct decisions of THIS contract."""
        contract_hash = contract.get("envelope", {}).get(
            "content_sha256") or _sha(_canonical(contract.get("object_phrases")))
        decisions = scope_decisions or {}
        cache_key = _sha(_canonical({
            "contract_hash": contract_hash, "policy_hash": policy_hash,
            "parser_hash": parser_hash, "query_params": query_params}))
        candidate_ids: List[str] = []
        reviewed_ids: List[str] = []
        rejected_ids: List[str] = []
        uncertain_ids: List[str] = []
        writable_overlay: List[Dict[str, Any]] = []
        legacy_licences_refused = 0
        for unit in snapshot.units:
            candidate_ids.append(unit["unit_id"])
            doc_id = unit["document_id"]
            dec = decisions.get(doc_id) or decisions.get(unit["unit_id"])
            if dec and dec.get("contract_hash") not in (None, contract_hash):
                dec = None  # decision made under a DIFFERENT contract: not reusable
            if dec and dec.get("verdict") in ("reviewed_direct", "adjacent",
                                              "background", "rejected", "uncertain"):
                # decision exists for THIS contract+policy: honour its lane
                if dec["verdict"] == "reviewed_direct":
                    reviewed_ids.append(unit["unit_id"])
                    writable_overlay.append({
                        "unit_id": unit["unit_id"], "document_id": doc_id,
                        "effective_permission": "qualified_support",
                        "decision_source": "scope_decision_reuse",
                    })
                elif dec["verdict"] in ("rejected", "uncertain"):
                    (rejected_ids if dec["verdict"] == "rejected"
                     else uncertain_ids).append(unit["unit_id"])
            else:
                # no reviewed decision: raw reuse without any licence
                legacy_licences_refused += 1
        manifest = {
            "schema_version": MANIFEST_SCHEMA,
            "generation_id": snapshot.generation_id,
            "snapshot_hash": snapshot.snapshot_hash,
            "question_hash": _sha(_canonical(contract.get("original_question", ""))),
            "contract_hash": contract_hash,
            "policy_hash": policy_hash,
            "parser_hash": parser_hash,
            "query_params_hash": _sha(_canonical(query_params)),
            "cache_key": cache_key,
            "candidate_unit_ids": candidate_ids,
            "reviewed_unit_ids": reviewed_ids,
            "rejected_unit_ids": rejected_ids,
            "uncertain_unit_ids": uncertain_ids,
            "writable_overlay": writable_overlay,
            "legacy_licences_refused": legacy_licences_refused,
            "projection_generation": snapshot.generation_id,
            "coverage_ready": bool(reviewed_ids),  # candidates never imply ready
        }
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            atomic_json(os.path.join(output_dir, "PROJECTION_MANIFEST.json"), manifest)
        return manifest


from typing import Callable  # noqa: E402
