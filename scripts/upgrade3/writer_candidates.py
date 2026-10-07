"""Explicit, additive writing-route experiments. Offline preview is the default.

No credential lookup or provider construction happens in preview or replay.
Only --run can reach the real Qwen transport, with a shared finite CNY ledger.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import math
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DEFAULT_CONFIG = PROJECT_ROOT / "config/writer_candidates/quality.json"
DEFAULT_TOKENIZER = PROJECT_ROOT / "data/tokenizers/qwen3_5_9b/tokenizer.json"
PROFILE_FIELDS = {
    "model", "thinking", "thinking_budget", "max_output_tokens", "json_mode",
    "timeout_seconds", "stream", "stream_overall_timeout_seconds",
    "prompt_token_multiplier", "prompt_token_framing_margin",
}
CALL_PROFILE_FIELDS = PROFILE_FIELDS - {"prompt_token_multiplier", "prompt_token_framing_margin"}
SECRET_FIELDS = {"api_key", "api_keys", "key_file", "password", "token", "access_token", "base_url", "authorization"}


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _no_secrets(value: Any) -> None:
    if isinstance(value, Mapping):
        if any(str(k).lower() in SECRET_FIELDS for k in value):
            raise ValueError("credentials_and_provider_destination_not_allowed_in_config")
        for child in value.values():
            _no_secrets(child)
    elif isinstance(value, list):
        for child in value:
            _no_secrets(child)


def resolve_packet_root(value: str | Path | None) -> Path | None:
    """Resolve a promoted CURRENT_PLAN pointer relative to its own directory."""
    if not value:
        return None
    supplied = Path(value).expanduser().resolve()
    current = supplied
    seen: set[Path] = set()
    while True:
        pointer = current if current.is_file() else current / "CURRENT_PLAN.json"
        if not pointer.is_file():
            if not current.is_dir():
                raise ValueError("packet_root_missing:" + str(current))
            return current
        if pointer.name != "CURRENT_PLAN.json":
            raise ValueError("packet_root_requires_directory_or_CURRENT_PLAN_json")
        if pointer in seen:
            raise ValueError("current_plan_pointer_cycle:" + str(pointer))
        seen.add(pointer)
        payload = read_json(pointer)
        target = payload.get("packet_root") if isinstance(payload, dict) else None
        if not isinstance(target, str) or not target.strip():
            raise ValueError("current_plan_packet_root_missing:" + str(pointer))
        current = Path(target).expanduser()
        if not current.is_absolute():
            current = pointer.parent / current
        current = current.resolve()


def tokenizer_counter(value: str | Path | None = None):
    """Use only local assets; the conservative fallback is never actual usage."""
    path = Path(value).expanduser().resolve() if value else DEFAULT_TOKENIZER
    if not path.is_file():
        if value:
            raise ValueError("explicit_local_tokenizer_missing:" + str(path))
        return None, {
            "mode": "utf8_byte_upper_bound", "tokenizer_sha256": None,
            "actual_provider_token_count": False,
            "warning": "Default local tokenizer is absent. UTF-8 byte upper bounds may reject or partition inputs that fit the model. Supply --tokenizer PATH for a local tokenizer estimate; no automatic download occurs.",
        }
    from optomind_research.runtime.upgrade3.progressive_review_plan import qwen_local_token_counter
    counter = qwen_local_token_counter(path)
    # Validate a supplied asset now, rather than after creating a paid attempt.
    counter(b"", [{"role": "user", "content": "local tokenizer validation"}])
    try:
        version = importlib.metadata.version("tokenizers")
    except importlib.metadata.PackageNotFoundError:
        version = None
    return counter, {
        "mode": "local_tokenizer_estimate", "tokenizer_sha256": sha256_file(path),
        "tokenizer_implementation_version": version, "actual_provider_token_count": False,
    }


class RecordingFactory:
    """Exact recorded content at the ordinary provider edge, never a fallback."""
    execution_mode = "recording"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        value = read_json(self.path)
        self.responses = value.get("responses", value) if isinstance(value, dict) else None
        if not isinstance(self.responses, dict) or not self.responses:
            raise ValueError("responses_must_be_nonempty_stage_mapping")
        self.calls: list[str] = []
        self.fixture_sha256 = sha256_file(path)

    def __call__(self, role: str, stage_dir: Path, profile: Mapping[str, Any]):
        def replay(messages, **kwargs):
            stage_id = str(kwargs.get("stage_id") or "")
            if not stage_id:
                # The attempt lives below its stable stage directory. This also
                # permits a caller to use the factory directly in offline checks.
                stage_id = next((part for part in reversed(Path(stage_dir).parts)
                                 if part.startswith(("writer_", "editor_"))), "")
            key = stage_id if stage_id in self.responses else role
            if key not in self.responses:
                raise ValueError("missing_recording_stage:" + (stage_id or role))
            self.calls.append(stage_id or role)
            recorded = copy.deepcopy(self.responses[key])
            if isinstance(recorded, Mapping) and "blocks" in recorded:
                response = {"content": json.dumps(recorded, ensure_ascii=False), "complete": True, "finish_reason": "stop"}
            elif isinstance(recorded, Mapping):
                response = dict(recorded)
            else:
                response = {"content": str(recorded), "complete": True, "finish_reason": "stop"}
            response.update(execution_mode="recording", current_run_cost_cny=0.0,
                            cost_provenance="offline_recording_no_provider_call",
                            recording_fixture_sha256=self.fixture_sha256,
                            recording_stage_key=key)
            return response
        replay.execution_mode = self.execution_mode
        return replay


def make_live_factory(args: argparse.Namespace, *, token_counter=None):
    """Create one lazy shared-ledger factory; no keys are read here."""
    if not args.run or args.responses:
        raise ValueError("live_execution_requires_run_without_responses")
    if not args.budget_ledger:
        raise ValueError("live_execution_requires_shared_budget_ledger")
    limit = args.budget_limit
    if limit is not None and (isinstance(limit, bool) or not math.isfinite(limit) or limit <= 0):
        raise ValueError("finite_positive_budget_limit_required")
    ledger_path = Path(args.budget_ledger).expanduser().resolve()
    if limit is None and not ledger_path.is_file():
        raise ValueError("new_ledger_requires_budget_limit")
    ledger = None

    def factory(role: str, stage_dir: Path, profile: Mapping[str, Any]):
        nonlocal ledger
        from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger, QwenDirectClient
        if ledger is None:
            ledger = GlobalBudgetLedger(limit_cny=limit, path=ledger_path)
            ledger._refresh_from_db()
            if ledger.limit_cny is None or not math.isfinite(ledger.limit_cny) or ledger.limit_cny <= 0:
                raise ValueError("existing_ledger_requires_finite_positive_limit")
        effective = {key: profile[key] for key in PROFILE_FIELDS if key in profile}
        constructor = {key: value for key, value in effective.items() if key != "stream"}
        provider = QwenDirectClient(
            **constructor, key_file=args.key_file, max_retries=0, max_keys=1,
            budget_ledger=ledger, prompt_token_counter=token_counter,
            raw_response_dir=Path(stage_dir) / "transport",
        )

        def call(messages, **kwargs):
            for key in CALL_PROFILE_FIELDS:
                if key not in effective:
                    continue
                if key in kwargs and kwargs[key] != effective[key]:
                    raise ValueError("effective_profile_override_mismatch:" + key)
                kwargs[key] = effective[key]
            # QwenDirectClient's constructor has no stream switch: it must
            # arrive at __call__, and the saved wire request verifies this.
            response = provider(messages, **kwargs)
            return {**response, "execution_mode": "live"}
        call.execution_mode = "live"
        for attribute in ("prompt_token_counter", "prompt_token_multiplier", "prompt_token_framing_margin",
                          "model", "json_mode", "max_retries", "timeout_seconds",
                          "stream_overall_timeout_seconds", "max_output_tokens", "thinking", "thinking_budget"):
            setattr(call, attribute, getattr(provider, attribute))
        return call

    factory.execution_mode = "live"
    factory.ledger_snapshot = lambda: ledger.as_dict() if ledger is not None else None
    return factory


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--arrangement", help="Accepted CHAPTER_ARRANGEMENT.json")
    source = p.add_mutually_exclusive_group()
    source.add_argument("--view", help="Explicit ARRANGEMENT_INPUT.json")
    source.add_argument("--packet-root", help="Packet directory, CURRENT_PLAN.json, or its parent; used when the sibling view is absent")
    p.add_argument("--route", choices=["chapter", "units_edit", "hierarchical"], default="chapter")
    p.add_argument("--output", required=True, help="Candidate or assembly artifact directory")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--language", default="zh")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true", help="Explicitly execute paid provider stages under the shared budget")
    mode.add_argument("--responses", help="Offline JSON stage-id/role to recorded response mapping")
    p.add_argument("--retry-failed", action="store_true", help="Explicitly permit a new attempt for failed cached stages")
    p.add_argument("--fallback-hierarchical", action="store_true", help="Explicitly permit a reported hierarchical capacity fallback")
    p.add_argument("--budget-ledger", help="One SQLite ledger shared by all compared routes and chapters")
    p.add_argument("--budget-limit", type=float, help="Finite total CNY cap; required for a new shared ledger")
    p.add_argument("--key-file", help="Local credentials; never copied to artifacts or read in offline modes")
    p.add_argument("--tokenizer", help="Existing local tokenizer.json; no downloads")
    p.add_argument("--assemble-manifest", help="Explicit ordered manifest of selected CHAPTER_RESULT.json paths")
    p.add_argument("--allow-pending-draft", action="store_true", help="Export incomplete selected prose with a visible draft label")
    return p


def _execution_context(output: Path, context: Mapping[str, Any]) -> None:
    path = output / "CLI_CONTEXT.json"
    if path.is_file():
        prior = read_json(path)
        old_mode, new_mode = prior.get("execution_mode"), context["execution_mode"]
        if old_mode not in (None, "preview", new_mode) and new_mode != "preview":
            raise ValueError("output_execution_mode_conflict:use_a_separate_output_directory")
        # A later preview must not erase the provenance of completed work.
        if new_mode == "preview" and old_mode not in (None, "preview"):
            return
    write_json(path, dict(context))


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.assemble_manifest:
            if args.arrangement or args.run or args.responses:
                raise ValueError("assembly_cannot_be_combined_with_candidate_execution")
            result = assemble_manifest(args.assemble_manifest, args.output, allow_pending_draft=args.allow_pending_draft)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if not args.arrangement:
            raise ValueError("arrangement_required")
        if args.allow_pending_draft:
            raise ValueError("allow_pending_draft_requires_assemble_manifest")
        from optomind_research.runtime.upgrade3.writer_candidates import run_candidate, validate_config
        from optomind_research.runtime.upgrade3.writer_candidates_contracts import build_chapter_input
        config = read_json(args.config)
        _no_secrets(config)
        config = validate_config(config)
        counter, meter = tokenizer_counter(args.tokenizer)
        packet_root = resolve_packet_root(args.packet_root)
        chapter = build_chapter_input(args.arrangement, view_path=args.view, packet_root=packet_root, language=args.language)
        mode = "live" if args.run else "recording" if args.responses else "preview"
        factory = make_live_factory(args, token_counter=counter) if args.run else RecordingFactory(args.responses) if args.responses else None
        output = Path(args.output).expanduser().resolve()
        context = {
            "schema_version": "optomind.writer_candidates_cli.v1", "execution_mode": mode,
            "arrangement_sha256": sha256_file(args.arrangement), "config": config,
            "meter": meter, "fixture_sha256": getattr(factory, "fixture_sha256", None),
            "packet_root": str(packet_root) if packet_root else None,
            "semantic_quality_unreviewed": True,
        }
        _execution_context(output, context)
        invocation = {**context, "requested_route": args.route, "retry_failed": args.retry_failed,
                      "fallback_hierarchical": args.fallback_hierarchical,
                      "started_at": datetime.now(timezone.utc).isoformat()}
        invocation_path = output / "cli_invocations" / (uuid.uuid4().hex + ".json")
        write_json(invocation_path, invocation)
        result = run_candidate(chapter, route=args.route, output_dir=output, config=config,
                               client_factory=factory, run=bool(args.run or args.responses),
                               retry_failed=args.retry_failed, fallback_hierarchical=args.fallback_hierarchical,
                               token_counter=counter)
        result = {**result, "execution_mode": mode, "meter": meter,
                  "cli_invocation": str(invocation_path), "semantic_quality_unreviewed": True}
        if factory is not None and hasattr(factory, "ledger_snapshot"):
            result["budget"] = factory.ledger_snapshot()
        if isinstance(factory, RecordingFactory):
            result["recorded_response_calls"] = factory.calls
            result["current_run_cost_cny"] = 0.0
        write_json(output / "CLI_RUN.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if mode == "preview" or result.get("complete") is True else 3
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


# The explicit assembly adapter is below. It consumes chapter results without
# reinterpreting cross-unit prose as legacy unit outputs.
def _assembly_source_identities(result: Mapping[str, Any], run: Mapping[str, Any], root: Path):
    """Use the hash-bound full input and the existing candidate identity rules."""
    input_hash = result.get("input_hash")
    if not isinstance(input_hash, str) or not input_hash:
        raise ValueError("assembly_input_hash_missing")
    local = root / "inputs" / input_hash / "CHAPTER_INPUT.json"
    input_path = local if local.is_file() else Path(str(run.get("input_path") or ""))
    if not input_path.is_file():
        raise ValueError("assembly_input_snapshot_missing:" + str(result.get("chapter_id")))
    chapter = read_json(input_path)
    encoded = json.dumps(chapter, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != input_hash:
        raise ValueError("assembly_input_snapshot_hash_mismatch:" + str(result.get("chapter_id")))
    if chapter.get("chapter_id") != result.get("chapter_id"):
        raise ValueError("assembly_input_chapter_identity_mismatch")
    from optomind_research.runtime.upgrade3.writer_candidates_contracts import _source_index
    catalog, aliases = _source_index(chapter.get("sources"))
    identities = []
    warnings = []
    for handle, source in catalog.items():
        identity = {key: copy.deepcopy(source[key]) for key in
                    ("canonical_paper_id", "paper_id", "doi", "title") if source.get(key)}
        if not any(identity.get(key) for key in ("canonical_paper_id", "paper_id", "doi")):
            warnings.append({"code": "source_stable_identity_missing", "chapter_id": result["chapter_id"],
                             "source_handle": handle, "detail": "Cross-chapter identity cannot be established from a title alone."})
        for citation_handle in [handle, *(alias for alias, canonical in aliases.items() if canonical == handle)]:
            identities.append({**identity, "source_handle": citation_handle, "aliases": []})
    return identities, warnings, str(input_path)


def assemble_manifest(manifest_path: str | Path, output_dir: str | Path, *, allow_pending_draft: bool = False) -> dict[str, Any]:
    """Assemble only explicitly selected chapter artifacts, in manifest order."""
    source = Path(manifest_path).expanduser().resolve()
    manifest = read_json(source)
    rows = manifest.get("chapters") if isinstance(manifest, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("assembly_requires_nonempty_ordered_chapters")
    output = Path(output_dir).expanduser().resolve()
    chapters = []
    seen_ids: set[str] = set()
    modes: set[str] = set()
    all_source_identities = []
    identity_warnings = []
    pieces = []
    title = manifest.get("review_title")
    if title:
        if not isinstance(title, str):
            raise ValueError("review_title_must_be_string")
        pieces.append("# " + title.strip())
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("result_path"), str):
            raise ValueError("assembly_requires_explicit_chapter_result_path")
        path = Path(row["result_path"]).expanduser()
        if not path.is_absolute():
            path = source.parent / path
        path = path.resolve()
        if path.name != "CHAPTER_RESULT.json":
            raise ValueError("assembly_only_accepts_CHAPTER_RESULT_json:" + str(path))
        result = read_json(path)
        if not isinstance(result, dict) or not str(result.get("schema_version", "")).startswith("optomind.writer_candidates"):
            raise ValueError("assembly_requires_candidate_chapter_schema:" + str(path))
        chapter_id = result.get("chapter_id")
        if not isinstance(chapter_id, str) or not chapter_id or row.get("chapter_id") != chapter_id:
            raise ValueError("assembly_chapter_identity_mismatch:" + str(path))
        if chapter_id in seen_ids:
            raise ValueError("assembly_duplicate_chapter:" + chapter_id)
        seen_ids.add(chapter_id)
        run_manifest_path = path.parent / "RUN_MANIFEST.json"
        if not run_manifest_path.is_file():
            raise ValueError("assembly_selected_run_manifest_missing:" + str(path))
        run = read_json(run_manifest_path)
        selected_version = run.get("selected_version")
        if not result.get("run_id") or selected_version != result["run_id"]:
            raise ValueError("assembly_result_is_not_selected_version:" + chapter_id)
        if run.get("chapter_id") != chapter_id:
            raise ValueError("assembly_run_chapter_identity_mismatch:" + chapter_id)
        root = path.parent.parent.parent if path.parent.parent.name == "runs" else path.parent
        identity_rows, identity_notes, input_path = _assembly_source_identities(result, run, root)
        all_source_identities.extend(identity_rows)
        identity_warnings.extend(identity_notes)
        context_path = root / "CLI_CONTEXT.json"
        context = read_json(context_path) if context_path.is_file() else {}
        mode = result.get("execution_mode") or run.get("execution_mode") or context.get("execution_mode")
        if mode not in {"live", "recording", "custom"}:
            raise ValueError("assembly_execution_mode_unverified:" + chapter_id)
        modes.add(mode)
        body = result.get("body_markdown")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("assembly_chapter_has_no_useful_body:" + chapter_id)
        pending_reasons = []
        if result.get("complete") is not True or result.get("pending_task_ids"):
            pending_reasons.append("chapter_result_pending")
        if run.get("status") != "complete":
            pending_reasons.append("current_run_" + str(run.get("status", "unknown")))
        if run.get("selected_input_matches_current") is not True or run.get("input_hash") != result.get("input_hash"):
            pending_reasons.append("selected_input_differs_from_current")
        if pending_reasons and not allow_pending_draft:
            raise ValueError("assembly_pending_chapter_requires_allow_pending_draft:" + chapter_id + ":" + ",".join(pending_reasons))
        chapter_title = row.get("title")
        if chapter_title is not None and not isinstance(chapter_title, str):
            raise ValueError("assembly_chapter_title_must_be_string:" + chapter_id)
        if chapter_title:
            leading = re.fullmatch(r"#{1,6}[ \t]+(.+?)[ \t]*", body.lstrip().splitlines()[0])
            if not leading or leading.group(1) != chapter_title.strip():
                pieces.append("## " + chapter_title.strip())
        pieces.append(body.strip())
        chapters.append({
            "chapter_id": chapter_id, "result_path": str(path), "result_sha256": sha256_file(path),
            "run_manifest_path": str(run_manifest_path), "run_manifest_sha256": sha256_file(run_manifest_path),
            "selected_version": selected_version, "input_hash": result.get("input_hash"), "input_path": input_path,
            "requested_route": result.get("requested_route"), "effective_route": result.get("effective_route"),
            "execution_mode": mode, "complete": not pending_reasons, "pending_reasons": pending_reasons,
            "pending_task_ids": result.get("pending_task_ids", []), "config": run.get("config"),
            "stage_lineage": result.get("stage_lineage", []),
        })
    if len(modes) != 1:
        raise ValueError("assembly_mixed_execution_modes_forbidden")
    from optomind_research.runtime.upgrade3.writer_candidates_contracts import _source_index
    # Expand each chapter's declared aliases into citation handles, then reuse
    # the same DOI/canonical-paper identity policy as the material consumer.
    # In particular, equal normalized DOI permits different historical IDs.
    source_identities, _ = _source_index(all_source_identities)
    complete = all(chapter["complete"] for chapter in chapters)
    if not complete:
        pieces.insert(0, "DRAFT: incomplete chapter work remains. See BODY_RESULT.json or PENDING_BODY_RESULT.json for pending tasks and provenance.")
    body = "\n\n".join(pieces) + "\n"
    result = {
        "schema_version": "optomind.writer_candidates_body.v1", "status": "complete" if complete else "pending_draft",
        "complete": complete, "execution_mode": next(iter(modes)), "semantic_quality_unreviewed": True,
        "chapters": chapters, "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "source_identity_map": source_identities, "identity_warnings": identity_warnings,
        "manifest_path": str(source), "manifest_sha256": sha256_file(source),
        "assembly_policy": "Explicit selected chapter results, ordered exactly by the manifest. No legacy unit split, rewriting, citation repair, or quality judgment.",
    }
    output.mkdir(parents=True, exist_ok=True)
    prior = read_json(output / "BODY_RESULT.json") if (output / "BODY_RESULT.json").is_file() else None
    preserve_complete = isinstance(prior, dict) and prior.get("complete") is True and not complete
    body_path = output / ("PENDING_BODY.md" if preserve_complete else "BODY.md")
    result_path = output / ("PENDING_BODY_RESULT.json" if preserve_complete else "BODY_RESULT.json")
    result.update(body_path=str(body_path), result_path=str(result_path), previous_complete_body_preserved=preserve_complete)
    temporary = body_path.with_name(body_path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(body, encoding="utf-8")
    os.replace(temporary, body_path)
    write_json(result_path, result)
    write_json(output / "ASSEMBLY_MANIFEST.json", manifest)
    write_json(output / "ASSEMBLY_REPORT.json", result)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
