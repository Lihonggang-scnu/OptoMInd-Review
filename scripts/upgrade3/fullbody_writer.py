"""Explicit complete-BODY candidates; preview and manifest preparation are offline.

--budget-limit is the shared ledger's ABSOLUTE lifetime CNY ceiling, never an
increment. All routes, retries, readers and revisions must share that ledger.
Only --run may construct the existing paid Qwen transport. No keys are read by
preview, manifest preparation or replay, and models never upgrade themselves.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.upgrade3 import writer_candidates as shared

DEFAULT_CONFIG = PROJECT_ROOT / "config/fullbody_writer/plus_first.json"
MANIFEST_SCHEMA = "optomind.fullbody_manifest.v1"
ROUTES = ("whole_author", "continuous_author", "workbench", "reader_revision",
          "plain_whole", "chapter_concat", "hierarchical_full")
read_json = shared.read_json
write_json = shared.write_json
sha256_file = shared.sha256_file
make_live_factory = shared.make_live_factory
tokenizer_counter = shared.tokenizer_counter


def _path(value: Any, base: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + "_path_required")
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), default=str).encode()).hexdigest()


def _chapter_ids(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value or any(
            not isinstance(item, str) or not item.strip() or item != item.strip() for item in value):
        raise ValueError(label + "_must_be_nonempty_chapter_list")
    if len(set(value)) != len(value):
        raise ValueError(label + "_duplicates")
    return list(value)


def _plan_ids(plan: Mapping[str, Any]) -> list[str] | None:
    # writer_packets is the producer's explicit BODY export list. Do not infer
    # BODY membership from incidental IDs, directory names or glob order.
    for field in ("writer_packets", "chapters", "shared_outline"):
        rows = plan.get(field)
        if isinstance(rows, list) and rows:
            ids = []
            for row in rows:
                if not isinstance(row, Mapping):
                    ids = []
                    break
                chapter = row.get("chapter") or row.get("chapter_plan") or {}
                identity = row.get("chapter_id") or (chapter.get("chapter_id") if isinstance(chapter, Mapping) else None)
                if not identity:
                    ids = []
                    break
                ids.append(identity)
            if ids:
                return _chapter_ids(ids, "approved_plan_chapters")
    return None


class _Files:
    """Snapshot actual file dependencies and validate optional prior hashes."""
    def __init__(self):
        self.rows: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _snapshot(path: Path, *, optional: bool) -> dict[str, Any]:
        try:
            if path.is_file():
                return {"path": str(path), "sha256": sha256_file(path), "exists": True}
        except OSError as exc:
            # A Windows locator is one long filename on POSIX and may fail
            # stat with ENAMETOOLONG. Preserve its exact identity and the
            # usable inline material; never guess a relocated substitute.
            if not optional:
                raise ValueError("manifest_file_unreadable:" + str(exc.errno) + ":" + str(path)) from exc
            return {"path": str(path), "sha256": None, "exists": False,
                    "unavailable_reason": "os_error", "unavailable_errno": exc.errno,
                    "unavailable_detail": exc.strerror}
        if not optional:
            raise ValueError("manifest_file_missing:" + str(path))
        return {"path": str(path), "sha256": None, "exists": False}

    def add(self, path: Path, *, expected: str | None = None, optional: bool = False,
            role: str = "input") -> dict[str, Any]:
        path = path.resolve()
        row = self._snapshot(path, optional=optional)
        if expected is not None and (not isinstance(expected, str) or row["sha256"] != expected):
            raise ValueError("manifest_file_hash_mismatch:" + str(path))
        prior = self.rows.get(str(path))
        if prior is not None and (prior["sha256"], prior["exists"]) != (row["sha256"], row["exists"]):
            raise ValueError("input_changed_during_preparation:" + str(path))
        row["roles"] = sorted(set((prior or {}).get("roles", [])) | {role})
        self.rows[str(path)] = row
        return row

    def verify(self) -> None:
        for row in list(self.rows.values()):
            current = Path(row["path"])
            fresh = self._snapshot(current, optional=not row["exists"])
            if (fresh["exists"], fresh["sha256"]) != (row["exists"], row["sha256"]):
                raise ValueError("input_changed_during_preparation:" + str(current))


def _resolve_root(path: Path, files: _Files) -> Path:
    seen: set[Path] = set()
    while True:
        pointer = path if path.is_file() else path / "CURRENT_PLAN.json"
        if not pointer.is_file():
            if not path.is_dir():
                raise ValueError("packet_root_missing:" + str(path))
            return path
        if pointer.name != "CURRENT_PLAN.json":
            raise ValueError("packet_root_requires_directory_or_CURRENT_PLAN_json")
        if pointer in seen:
            raise ValueError("current_plan_pointer_cycle:" + str(pointer))
        seen.add(pointer)
        files.add(pointer, role="current_plan_pointer")
        payload = read_json(pointer)
        target = payload.get("packet_root") if isinstance(payload, Mapping) else None
        path = _path(target, pointer.parent, "current_plan_packet_root")


def _locator_files(arrangement: Mapping[str, Any], files: _Files) -> None:
    # These are the exact local locator fields consumed by build_unit_view.
    # Relative embedded locators use the legacy builder's CWD semantics;
    # manifest paths themselves always resolve relative to the manifest.
    catalog = arrangement.get("source_catalog") or {}
    for source in catalog.values() if isinstance(catalog, Mapping) else ():
        locator = source.get("locator") if isinstance(source, Mapping) else None
        if not isinstance(locator, Mapping):
            continue
        for key in ("writer_packet", "card_path"):
            if locator.get(key):
                path = _path(locator[key], Path.cwd(), "locator")
                files.add(path, optional=True, role="locator_" + key)


def _build_chapter(arrangement: Path, view: Path | None, root: Path, plan: Mapping[str, Any],
                   language: str, files: _Files) -> dict[str, Any]:
    from optomind_research.runtime.upgrade3.writer_candidates_contracts import build_chapter_input
    if view is not None:
        return build_chapter_input(arrangement, view_path=view, packet_root=root, language=language)
    # Build the same production packet view in scratch space. This avoids the
    # legacy builder silently selecting a sibling (possibly older) view, or
    # adding legacy IDs to the accepted input directory during an offline read.
    from optomind_research.runtime.upgrade3.chapter_arrangement import build_chapter_view
    chapter_id = read_json(arrangement).get("chapter_id")
    packet = root / "writer_packets" / (str(chapter_id) + ".json")
    files.add(packet, role="writer_packet")
    id_map = root / "chapter_arrangement/ID_MAP.json"
    files.add(id_map, optional=True, role="legacy_identity_map")
    with tempfile.TemporaryDirectory(prefix="fullbody-view-") as temporary:
        scratch = Path(temporary)
        scratch_ids = scratch / "ID_MAP.json"
        if id_map.is_file():
            scratch_ids.write_bytes(id_map.read_bytes())
        generated = build_chapter_view(packet, shared_outline=plan.get("shared_outline") or [],
            review_argument=str(plan.get("review_argument") or ""), shared_scope=plan.get("shared_scope"),
            review_argument_status=str(plan.get("review_argument_status") or ""),
            review_argument_source=str(plan.get("review_argument_source") or ""),
            id_map_path=scratch_ids).to_dict(include_material=False)
        # The generated view's locator is navigation metadata, not a stable
        # input dependency. Keep all other production view content unchanged.
        generated["id_map_path"] = str(id_map)
        scratch_view = scratch / "ARRANGEMENT_INPUT.json"
        write_json(scratch_view, generated)
        chapter = build_chapter_input(arrangement, view_path=scratch_view, packet_root=root, language=language)
        chapter["provenance"].update(view_path="", view_origin="explicit_writer_packet",
            generated_view_sha256=_hash(generated), packet_path=str(packet), packet_sha256=sha256_file(packet))
        chapter["provenance"].pop("view_sha256", None)
        return chapter


def load_body_manifest(path: str | Path, *, plan_path: str | Path | None = None,
                       language: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read explicit accepted chapters, pin their actual files, and build BODY."""
    from optomind_research.runtime.upgrade3.fullbody_contracts import build_fullbody_input, seal_fullbody_input
    source = Path(path).expanduser().resolve()
    manifest = read_json(source)
    if not isinstance(manifest, Mapping) or manifest.get("schema_version", MANIFEST_SCHEMA) != MANIFEST_SCHEMA:
        raise ValueError("fullbody_manifest_schema_required")
    rows = manifest.get("chapters")
    if not isinstance(rows, list) or not rows or any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("manifest_requires_explicit_ordered_chapters")
    actual = _chapter_ids([row.get("chapter_id") for row in rows], "manifest_chapters")
    files = _Files()
    # A prepared manifest binds both present and absent local dependencies.
    for row in manifest.get("source_files", []):
        if not isinstance(row, Mapping):
            raise ValueError("manifest_source_file_not_object")
        current = _path(row.get("path"), source.parent, "source_file")
        observed = files.add(current, expected=row.get("sha256"), optional=row.get("exists") is False,
                             role="prepared_source")
        if row.get("exists") is False and observed["exists"]:
            raise ValueError("previously_missing_source_now_exists:" + str(current))
    explicit_plan = plan_path or manifest.get("plan_path")
    plan: dict[str, Any] = {}
    plan_file: Path | None = None
    default_root: Path | None = None
    if explicit_plan:
        chosen = _path(str(explicit_plan), Path.cwd() if plan_path else source.parent, "plan")
        if chosen.is_dir() or chosen.name == "CURRENT_PLAN.json":
            default_root = _resolve_root(chosen, files)
            plan_file = default_root / "DETAILED_REVIEW_PLAN.json"
        else:
            plan_file, default_root = chosen, chosen.parent
        files.add(plan_file, expected=manifest.get("plan_sha256"), role="approved_plan")
        plan = read_json(plan_file)
        if not isinstance(plan, dict):
            raise ValueError("approved_plan_must_be_object")
    prepared_rows = []
    chapters = []
    observed_plan_ids: list[list[str]] = []
    if plan:
        approved_ids = _plan_ids(plan)
        if approved_ids:
            observed_plan_ids.append(approved_ids)
    for row in rows:
        chapter_id = row["chapter_id"]
        arrangement = _path(row.get("arrangement_path"), source.parent, "arrangement")
        files.add(arrangement, expected=row.get("arrangement_sha256"), role="accepted_arrangement")
        arrangement_data = read_json(arrangement)
        if not isinstance(arrangement_data, Mapping) or arrangement_data.get("chapter_id") != chapter_id:
            raise ValueError("manifest_arrangement_chapter_identity_mismatch:" + chapter_id)
        root_value = row.get("packet_root") or manifest.get("packet_root")
        root = _resolve_root(_path(root_value, source.parent, "packet_root"), files) if root_value else default_root
        if root is None:
            raise ValueError("manifest_explicit_packet_root_required:" + chapter_id)
        local_plan_file = root / "DETAILED_REVIEW_PLAN.json"
        files.add(local_plan_file, optional=True, role="packet_plan")
        local_plan = read_json(local_plan_file) if local_plan_file.is_file() else {}
        if not isinstance(local_plan, Mapping):
            raise ValueError("packet_plan_must_be_object")
        local_ids = _plan_ids(local_plan)
        if local_ids:
            observed_plan_ids.append(local_ids)
        if not plan and local_plan:
            plan, plan_file = dict(local_plan), local_plan_file
        packet = root / "writer_packets" / (chapter_id + ".json")
        files.add(packet, role="writer_packet")
        packet_data = read_json(packet)
        if not isinstance(packet_data, Mapping):
            raise ValueError("writer_packet_must_be_object")
        packet_chapter = packet_data.get("chapter") or {}
        if not isinstance(packet_chapter, Mapping):
            raise ValueError("writer_packet_chapter_must_be_object")
        packet_identity = packet_data.get("chapter_id") or packet_chapter.get("chapter_id")
        if packet_identity and packet_identity != chapter_id:
            raise ValueError("manifest_packet_chapter_identity_mismatch:" + chapter_id)
        packet_files = row.get("packet_files", [])
        if not isinstance(packet_files, list) or any(not isinstance(item, Mapping) for item in packet_files):
            raise ValueError("manifest_packet_files_must_be_file_list")
        for item in packet_files:
            files.add(_path(item.get("path"), root, "packet_file"), expected=item.get("sha256"), role="explicit_packet_file")
        view = _path(row["view_path"], source.parent, "view") if row.get("view_path") else None
        if view:
            files.add(view, expected=row.get("view_sha256"), role="accepted_view")
            view_data = read_json(view)
            if not isinstance(view_data, Mapping) or view_data.get("chapter_id") != chapter_id:
                raise ValueError("manifest_view_chapter_identity_mismatch:" + chapter_id)
        _locator_files(arrangement_data, files)
        effective_language = language or manifest.get("language") or plan.get("language") or local_plan.get("language") or "zh"
        chapter = _build_chapter(arrangement, view, root, local_plan or plan, effective_language, files)
        chapters.append(chapter)
        prepared_rows.append({"chapter_id": chapter_id, "arrangement_path": str(arrangement),
            "arrangement_sha256": sha256_file(arrangement), "packet_root": str(root),
            **({"view_path": str(view), "view_sha256": sha256_file(view)} if view else {})})
    expected = manifest.get("expected_chapter_ids") or (observed_plan_ids[0] if observed_plan_ids else None)
    expected = _chapter_ids(expected, "expected_chapter_ids")
    if actual != expected:
        raise ValueError("fullbody_chapter_scope_or_order_mismatch:expected=" + repr(expected) + ":actual=" + repr(actual))
    for approved_ids in observed_plan_ids:
        if approved_ids != expected:
            raise ValueError("approved_plan_fullbody_chapter_scope_mismatch:expected=" + repr(approved_ids))
    files.verify()
    prepared = {"schema_version": MANIFEST_SCHEMA, "expected_chapter_ids": expected,
        "actual_chapter_ids": actual, "chapters": prepared_rows, "language": effective_language,
        "scope_authority": "approved_plan" if observed_plan_ids else "explicit_manifest_list_user_responsibility",
        "research_question": manifest.get("research_question") or plan.get("research_question") or "",
        "review_argument": manifest.get("review_argument") or plan.get("review_argument") or "",
        "shared_scope": copy.deepcopy(manifest.get("shared_scope") or manifest.get("review_scope") or plan.get("shared_scope") or plan.get("review_scope") or {}),
        "original_user_request": copy.deepcopy(manifest.get("original_user_request") or manifest.get("user_request") or plan.get("original_user_request") or plan.get("user_request") or ""),
        "target_reader": copy.deepcopy(manifest.get("target_reader") or plan.get("target_reader") or ""),
        "source_files": [{key: value for key, value in files.rows[p].items() if key != "roles"}
                         for p in sorted(files.rows)],
        **({"plan_path": str(plan_file), "plan_sha256": sha256_file(plan_file)} if plan_file else {})}
    book = build_fullbody_input(chapters, research_question=prepared["research_question"] or None,
        review_argument=prepared["review_argument"] or None, language=effective_language,
        original_user_request=prepared["original_user_request"] or None,
        target_reader=prepared["target_reader"] or None,
        shared_scope=prepared["shared_scope"] or None,
        approved_plan={"path": str(plan_file) if plan_file else None,
            "sha256": sha256_file(plan_file) if plan_file else None,
            "chapter_order": expected, "shared_outline": copy.deepcopy(plan.get("shared_outline") or []),
            "review_argument_status": plan.get("review_argument_status"),
            "review_argument_source": plan.get("review_argument_source")})
    book["input_manifest"]["fullbody_manifest"] = copy.deepcopy(prepared)
    book["input_manifest"]["fullbody_manifest_sha256"] = _hash(prepared)
    book["expected_chapter_ids"] = expected
    book["actual_chapter_ids"] = actual
    return seal_fullbody_input(book), prepared


class RecordingFactory(shared.RecordingFactory):
    """Full-BODY response objects, stage keys or role keys; no live fallback."""
    def __init__(self, path: str | Path):
        super().__init__(path)
        for key, value in list(self.responses.items()):
            if isinstance(value, Mapping) and "content" not in value:
                self.responses[key] = {"content": json.dumps(value, ensure_ascii=False),
                                       "complete": True, "finish_reason": "stop"}


def load_draft(path: str | Path, *, route: str = "reader_revision") -> dict[str, Any]:
    """Load an exact complete BODY, retaining its lineage and scientific scope."""
    if route not in ("reader_revision", "hierarchical_full"):
        raise ValueError("draft_only_supported_for_reader_revision_or_hierarchical_full")
    source = Path(path).expanduser().resolve()
    allowed_names = {"FULL_BODY_RESULT.json"}
    if route == "hierarchical_full":
        allowed_names.add("INDEPENDENT_FULL_BODY_RESULT.json")
    if source.name not in allowed_names:
        raise ValueError(route + "_requires_FULL_BODY_RESULT_json")
    result = read_json(source)
    if not isinstance(result, dict) or result.get("complete") is not True or result.get("pending_task_ids"):
        raise ValueError(route + "_requires_complete_fullbody_draft")
    if route == "hierarchical_full" and (result.get("effective_route") or result.get("requested_route")) != "chapter_concat":
        raise ValueError("hierarchical_full_requires_chapter_concat_draft")
    body = result.get("body_markdown")
    if not isinstance(body, str) or not body.strip():
        raise ValueError(route + "_fullbody_draft_has_no_body")
    digest = hashlib.sha256(body.encode()).hexdigest()
    if result.get("body_sha256") and result["body_sha256"] != digest:
        raise ValueError(route + "_draft_body_hash_mismatch")
    result["_loaded_from"] = {"path": str(source), "sha256": sha256_file(source), "body_sha256": digest}
    return result


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", required=True, help="Explicit ordered complete-BODY input manifest")
    p.add_argument("--plan", help="Explicit approved DETAILED_REVIEW_PLAN.json, packet directory or CURRENT_PLAN.json")
    p.add_argument("--prepare-manifest", help="Write a hash-pinned offline manifest and exit; --output is unnecessary")
    p.add_argument("--route", choices=ROUTES, default="whole_author")
    p.add_argument("--output", help="Candidate artifact directory; reuse it only with the same pinned input versions")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--language", help="Explicit language override; otherwise manifest, approved plan, then zh")
    p.add_argument("--draft", help="Complete FULL_BODY_RESULT.json: required for reader_revision; optional chapter_concat result for hierarchical_full (also INDEPENDENT_FULL_BODY_RESULT.json)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true", help="Explicit paid execution; requires one shared finite CNY ledger")
    mode.add_argument("--responses", help="Offline JSON stage-id or role to recorded response mapping")
    p.add_argument("--allow-max", action="store_true", help="Explicit paid Max permission; never selects or upgrades a model")
    p.add_argument("--retry-failed", action="store_true", help="Explicitly allow another attempt for failed or uncertain stages")
    p.add_argument("--continue-incomplete", action="store_true", help="Explicitly continue saved finish_reason=length prose in continuous_author/workbench; not supported by plain baselines")
    p.add_argument("--budget-ledger", help="One SQLite ledger shared by all advanced/plain routes, continuations and revisions")
    p.add_argument("--budget-limit", type=float, help="ABSOLUTE lifetime CNY cap, not additional credit; existing ledger cap cannot silently change")
    p.add_argument("--key-file", help="Local credentials, read only by an actual --run transport call")
    p.add_argument("--tokenizer", help="Existing local tokenizer.json; no download or network lookup")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.prepare_manifest:
            if args.run or args.responses or args.draft:
                raise ValueError("prepare_manifest_is_offline_only")
            if Path(args.prepare_manifest).expanduser().resolve() == Path(args.manifest).expanduser().resolve():
                raise ValueError("prepare_manifest_requires_new_destination")
            _, prepared = load_body_manifest(args.manifest, plan_path=args.plan, language=args.language)
            write_json(Path(args.prepare_manifest).expanduser().resolve(), prepared)
            print(json.dumps({"prepared_manifest": str(Path(args.prepare_manifest).resolve()),
                "manifest_sha256": sha256_file(args.prepare_manifest), "chapter_ids": prepared["actual_chapter_ids"],
                "source_file_count": len(prepared["source_files"]), "execution_mode": "preview"}, indent=2))
            return 0
        if not args.output:
            raise ValueError("output_required")
        if args.continue_incomplete and args.route in ("plain_whole", "chapter_concat", "hierarchical_full"):
            raise ValueError("continue_incomplete_not_supported_for_plain_routes:use continuous_author or workbench for prefix continuation")
        if args.route == "reader_revision" and not args.draft:
            raise ValueError("reader_revision_requires_explicit_draft")
        if args.route not in ("reader_revision", "hierarchical_full") and args.draft:
            raise ValueError("draft_only_supported_for_reader_revision_or_hierarchical_full")
        from optomind_research.runtime.upgrade3.fullbody_writer import run_fullbody_candidate, validate_config
        config = read_json(args.config)
        shared._no_secrets(config)
        config = validate_config(config)
        if args.route == "reader_revision":
            roles = ("reader", "reviser")
        elif args.route == "hierarchical_full":
            roles = ("reviser",) if args.draft else ("writer", "reviser")
        else:
            roles = ("writer",)
        shared._require_max_permission(args, {role: config[role] for role in roles})
        book, prepared = load_body_manifest(args.manifest, plan_path=args.plan, language=args.language)
        base_result = load_draft(args.draft, route=args.route) if args.draft else None
        if base_result is not None and base_result.get("input_hash") != _hash(book):
            raise ValueError(args.route + "_draft_input_hash_mismatch")
        if args.run and base_result is not None and base_result.get("execution_mode") != "live":
            raise ValueError("live_" + args.route + "_requires_live_fullbody_draft:use preview or --responses for recorded drafts")
        counter, meter = tokenizer_counter(args.tokenizer)
        mode = "live" if args.run else "recording" if args.responses else "preview"
        output = Path(args.output).expanduser().resolve()
        snapshot = output / "SOURCE_MANIFEST.json"
        if snapshot.is_file() and _hash(read_json(snapshot)) != _hash(prepared):
            raise ValueError("input_source_versions_changed:prepare a new manifest and use a new output directory")
        factory = make_live_factory(args, token_counter=counter) if args.run else RecordingFactory(args.responses) if args.responses else None
        context = {"schema_version": "optomind.fullbody_writer_cli.v1", "execution_mode": mode,
            "manifest_path": str(Path(args.manifest).expanduser().resolve()),
            "manifest_sha256": sha256_file(args.manifest), "source_manifest_sha256": _hash(prepared),
            "expected_chapter_ids": prepared["expected_chapter_ids"], "actual_chapter_ids": prepared["actual_chapter_ids"],
            "config": config, "meter": meter, "fixture_sha256": getattr(factory, "fixture_sha256", None),
            "draft": base_result.get("_loaded_from") if base_result else None, "semantic_quality_unreviewed": True,
            "budget_limit_semantics": "absolute_shared_ledger_lifetime_CNY_ceiling_not_incremental_credit"}
        shared._execution_context(output, context)
        write_json(snapshot, prepared)
        invocation_path = output / "cli_invocations" / (uuid.uuid4().hex + ".json")
        write_json(invocation_path, {**context, "requested_route": args.route, "allow_max": args.allow_max,
            "retry_failed": args.retry_failed, "continue_incomplete": args.continue_incomplete,
            "started_at": datetime.now(timezone.utc).isoformat()})
        result = run_fullbody_candidate(book, route=args.route, output_dir=output, config=config,
            client_factory=factory, run=bool(args.run or args.responses), retry_failed=args.retry_failed,
            token_counter=counter, base_result=base_result, continue_incomplete=args.continue_incomplete)
        result = {**result, "execution_mode": mode, "meter": meter, "cli_invocation": str(invocation_path),
            "source_manifest_path": str(snapshot), "source_manifest_sha256": _hash(prepared),
            "expected_chapter_ids": prepared["expected_chapter_ids"], "actual_chapter_ids": prepared["actual_chapter_ids"],
            "semantic_quality_unreviewed": True}
        if factory is not None and hasattr(factory, "ledger_snapshot"):
            result["budget"] = factory.ledger_snapshot()
        if isinstance(factory, RecordingFactory):
            result.update(recorded_response_calls=factory.calls, current_run_cost_cny=0.0)
        write_json(output / "CLI_RUN.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if mode == "preview" or result.get("complete") is True else 3
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
