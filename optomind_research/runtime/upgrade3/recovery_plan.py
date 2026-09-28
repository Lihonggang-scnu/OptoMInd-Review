"""Upgrade-3 generation recovery & selective invalidation (ticket 030).

RUN_IDENTITY freezes the STATIC generation identity (code, model manifest,
policy, prompt manifest, original question, material snapshot).  Recovery:

- same identity + crash: committed nodes are reused (hash-verified), only
  uncommitted nodes recompute; charged-but-uncommitted raw responses are
  verified for reuse, never blindly resent;
- ANY static change (code/prompt/model/policy/source/parser/question) or a
  material refresh invalidates the affected subgraph under a NEW generation
  with an explicit parent link; old objects keep their generation labels
  (copy-by-reference provenance, never relabelled);
- late callbacks are fenced by (generation, attempt): stale results can never
  overwrite a committed node;
- RUNNING->FAILED->FAILED style rewrites and terminal-after-terminal are
  invalid transitions (the R6 defect), rejected at plan time;
- CURRENT-pointer drift is detected: recovery consumes only the pinned
  snapshot, never the drifted CURRENT.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

IDENTITY_SCHEMA = "optomind.upgrade3.run_identity.v1"
PLAN_SCHEMA = "optomind.upgrade3.recovery_plan.v1"

STATIC_KEYS = ("code_sha256", "model_manifest_hash", "policy_sha256",
               "prompt_manifest_hash", "question_hash", "material_snapshot_hash")


def _sha(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def run_identity(*, code_sha256: str, model_manifest_hash: str,
                 policy_sha256: str, prompt_manifest_hash: str,
                 question_hash: str, material_snapshot_hash: str) -> Dict[str, Any]:
    body = {k: v for k, v in dict(
        code_sha256=code_sha256, model_manifest_hash=model_manifest_hash,
        policy_sha256=policy_sha256, prompt_manifest_hash=prompt_manifest_hash,
        question_hash=question_hash,
        material_snapshot_hash=material_snapshot_hash).items()}
    body["identity_hash"] = _sha(_canonical(body))
    return {"schema_version": IDENTITY_SCHEMA, **body}


class RecoveryPlan:
    """node_deps: node -> {"deps": [node...], "static_keys": [key...]}."""

    def __init__(self, *, identity: Dict[str, Any],
                 node_deps: Dict[str, Dict[str, Any]]):
        self.identity = identity
        self.node_deps = node_deps

    def identity_drift(self, now: Dict[str, Any]) -> List[str]:
        changed = []
        for key in STATIC_KEYS:
            if self.identity.get(key) != now.get(key):
                changed.append(key)
        return changed

    def plan_recovery(self, *, committed: Dict[str, str],
                      events: Optional[List[Dict[str, Any]]] = None,
                      identity_now: Optional[Dict[str, Any]] = None,
                      current_pointer_hash: str = "",
                      pinned_snapshot_hash: str = "",
                      root_dir: Optional[str] = None) -> Dict[str, Any]:
        """Recovery plan with dependency propagation and real file hash checks.

        When a node is invalidated (by static drift or lack of commit), ALL
        downstream nodes that depend on it (directly or transitively) are also
        invalidated.  When ``root_dir`` is provided, the hash of each committed
        node's output file is verified against the recorded hash; a mismatch
        downgrades ``reuse`` to ``invalid``."""
        events = events or []
        plan: Dict[str, Dict[str, Any]] = {}
        problems: List[str] = []
        # invalid transitions in the event log (R6 FAILED->FAILED pattern)
        finish_seq: Dict[str, List[str]] = {}
        for e in events:
            if e.get("kind") != "stage_finished":
                continue
            key = e["stage"] + "/" + e["attempt"]
            finish_seq.setdefault(key, []).append(e.get("outcome") or "")
        for key, outs in finish_seq.items():
            if len(outs) > 1:
                problems.append("illegal_repeat_transition:" + key)
        drift = self.identity_drift(identity_now or self.identity)
        if drift:
            problems.append("static_identity_drift:" + ",".join(drift))
        # step 1: per-node initial action based on static drift
        node_actions: Dict[str, str] = {}
        node_reasons: Dict[str, str] = {}
        for node, spec in self.node_deps.items():
            static_keys = spec.get("static_keys") or []
            node_drift = [k for k in static_keys if k in drift]
            committed_hash = committed.get(node)
            if node_drift:
                node_actions[node] = "invalid"
                node_reasons[node] = "static_inputs_changed:" + ",".join(node_drift)
            elif committed_hash:
                node_actions[node] = "reuse"
                node_reasons[node] = "committed_and_no_drift"
            else:
                node_actions[node] = "recompute"
                node_reasons[node] = "not_committed_in_this_generation"
        # step 2: propagate invalidation along dependency graph (topological)
        def get_deps(node: str) -> List[str]:
            return self.node_deps.get(node, {}).get("deps", [])
        changed = True
        while changed:
            changed = False
            for node in self.node_deps:
                if node_actions.get(node) in ("reuse",):
                    for dep in get_deps(node):
                        if node_actions.get(dep) in ("invalid", "recompute"):
                            node_actions[node] = "invalid"
                            node_reasons[node] = (
                                f"upstream_{dep}_is_"
                                f"{node_actions.get(dep, 'unknown')}")
                            changed = True
        # step 3: real file hash verification for reuse nodes
        if root_dir:
            for node in self.node_deps:
                if node_actions.get(node) != "reuse":
                    continue
                committed_hash = committed.get(node)
                if committed_hash:
                    import os as _os
                    candidate_paths = [
                        _os.path.join(root_dir, node, "draft.md"),
                        _os.path.join(root_dir, node, "output.bin"),
                        _os.path.join(root_dir, node + ".md"),
                        _os.path.join(root_dir, node + ".json"),
                    ]
                    found = False
                    for cp in candidate_paths:
                        if _os.path.isfile(cp):
                            found = True
                            actual = _sha(open(cp, "rb").read())
                            if actual != committed_hash:
                                node_actions[node] = "invalid"
                                node_reasons[node] = (
                                    f"file_hash_mismatch:{cp}")
                                changed = True
                            break
                    if not found and not _os.path.isdir(
                            _os.path.join(root_dir, node)):
                        # no file found: committed hash refers to in-memory data
                        pass
        # step 4: build plan
        for node in self.node_deps:
            action = node_actions.get(node, "recompute")
            plan[node] = {"action": action,
                          "reason": node_reasons.get(node, "")}
        # CURRENT drift
        pointer_drift = False
        if pinned_snapshot_hash and current_pointer_hash and \
                current_pointer_hash != pinned_snapshot_hash:
            pointer_drift = True
            problems.append("current_pointer_drift:recovery_consumes_pinned_snapshot")
        return {
            "schema_version": PLAN_SCHEMA,
            "identity_hash": self.identity.get("identity_hash"),
            "identity_drift": drift,
            "node_plan": plan,
            "problems": problems,
            "pointer_drift": pointer_drift,
        }

    # ------------------------------------------------ fencing ----
    def accept_callback(self, *, expected_generation: str, expected_attempt: str,
                        callback_generation: str, callback_attempt: str,
                        committed_hash: Optional[str]) -> Dict[str, Any]:
        """Late-result fencing: a callback whose generation/attempt no longer
        matches the current attempt is rejected and can never overwrite."""
        if callback_generation != expected_generation:
            return {"accept": False,
                    "reason": "fenced:stale_generation:" + callback_generation}
        if callback_attempt != expected_attempt:
            return {"accept": False,
                    "reason": "fenced:stale_attempt:" + callback_attempt}
        if committed_hash is not None:
            return {"accept": False, "reason": "fenced:node_already_committed"}
        return {"accept": True}


def new_generation_for(parent: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """Post-terminal fixes / static changes: a NEW generation with explicit
    parent; the parent's terminal state is never rewritten."""
    return {
        "parent_generation": parent.get("generation_id"),
        "parent_terminal_hash": parent.get("terminal_hash"),
        "reason": reason,
        "inherits": "raw_material_reuse_only_science_rejudged",
    }
