"""Build immutable local material snapshots or run the bounded acquisition batch.

The command keeps the provider layer separate from material reading. It can
consume a local JATS/TEI/PDF, a prepared snapshot, one metadata record, or a
frozen manifest containing ``records``. Progress lines are ASCII-safe JSON so
the command remains usable from Windows consoles with legacy code pages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Mapping

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from optomind_research.runtime.upgrade3.document_snapshot import NORMALIZER_VERSION
from optomind_research.runtime.upgrade3.local_materials import PreparedSnapshotProvider
from optomind_research.runtime.upgrade3.material_acquisition import ACQUISITION_SCHEMA_VERSION, AcquisitionConfig, AcquisitionResult, MaterialAcquirer
from optomind_research.runtime.upgrade3.public_reader import FirecrawlReader


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _record(path: Path | None, canonical_paper_id: str, title: str = "") -> dict[str, Any]:
    value: Any = _read_json(path) if path else {}
    if not isinstance(value, Mapping):
        raise ValueError("record_json_must_be_object")
    row = dict(value)
    if canonical_paper_id and not row.get("canonical_paper_id"):
        row["canonical_paper_id"] = canonical_paper_id
    if title and not row.get("title"):
        row["title"] = title
    return row


def _safe_slug(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "paper"))[:120] or "paper"


def _summary(result: Any) -> dict[str, Any]:
    identity = result.identity.as_mapping()
    return {
        "paper_id": identity.get("canonical_paper_id", ""),
        "sample_id": identity.get("sample_id", ""),
        "status": result.status,
        "material_depth": result.material_depth,
        "source": result.selected_route,
        "snapshot_id": result.snapshot.snapshot_id if result.snapshot else "",
        "snapshot_path": str(result.snapshot.root) if result.snapshot else "",
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "stage_timings": {key: round(float(value), 3) for key, value in result.stage_timings.items()},
        "errors": list(result.errors),
    }


def _print(value: Mapping[str, Any]) -> None:
    print(json.dumps(dict(value), ensure_ascii=True, sort_keys=True), flush=True)


def _implementation_fingerprint() -> dict[str, str]:
    paths = (
        REPO / "optomind_research/runtime/upgrade3/material_acquisition.py",
        REPO / "optomind_research/runtime/upgrade3/public_fulltext_rescue.py",
        REPO / "optomind_research/runtime/upgrade3/public_reader.py",
        REPO / "optomind_research/runtime/upgrade3/document_snapshot.py",
        REPO / "optomind_research/runtime/upgrade3/docling_adapter.py",
        REPO / "optomind_research/runtime/upgrade3/regional_recognition.py",
        REPO / "scripts/upgrade3/parse_docling_pdf.py",
        REPO / "optomind_research/runtime/upgrade3/local_materials.py",
        REPO / "tools/academic_backends/grobid_backend.py",
        REPO / "tools/academic_backends/core_backend.py",
        REPO / "tools/academic_backends/openalex_backend.py",
        REPO / "tools/academic_backends/unpaywall_backend.py",
        REPO / "tools/academic_backends/arxiv_backend.py",
        Path(__file__).resolve(),
    )
    return {str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths if path.is_file()}


def _fingerprint(record: Mapping[str, Any], config: AcquisitionConfig, *, source_bytes: bytes = b"", xml_bytes: bytes = b"", pdf_bytes: bytes = b"") -> str:
    # ``source_bytes`` remains a compatibility alias for callers using the
    # pre-paired-input helper.  New checkpoints distinguish XML and PDF bytes.
    if source_bytes and not (xml_bytes or pdf_bytes):
        xml_bytes = source_bytes
    payload = {
        "record": dict(record),
        "source_sha256": {
            "xml": hashlib.sha256(xml_bytes).hexdigest() if xml_bytes else "",
            "pdf": hashlib.sha256(pdf_bytes).hexdigest() if pdf_bytes else "",
        },
        "normalizer_version": NORMALIZER_VERSION,
        "acquisition_schema_version": ACQUISITION_SCHEMA_VERSION,
        "config": dict(vars(config)),
        "grobid": {
            "service_url": os.environ.get("GROBID_URL", "http://127.0.0.1:8070"),
            "image": "grobid/grobid:0.9.1-crf",
            "coordinate_fields": ["p", "figure", "ref", "formula", "biblStruct"],
        },
        "implementation": _implementation_fingerprint(),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _checkpoint_path(root: Path, paper_id: Any) -> Path:
    return root / "_batch_checkpoints" / f"{_safe_slug(paper_id)}.json"


def _regional_completion(snapshot: Any) -> tuple[bool, dict[str, int]]:
    statuses: dict[str, int] = {}
    for asset in snapshot.assets:
        if asset.get("parser") == "docling" and asset.get("type") in {"formula", "table"}:
            status = str(asset.get("recognition_status") or "parser_candidate")
            statuses[status] = statuses.get(status, 0) + 1
    return all(status in {"recognized", "uncertain"} for status in statuses), statuses


def _load_resume(root: Path, paper_id: Any, fingerprint: str, *, refresh: bool) -> dict[str, Any] | None:
    if refresh:
        return None
    path = _checkpoint_path(root, paper_id)
    if not path.is_file():
        return None
    try:
        payload = _read_json(path)
        snapshot_path = Path(str(payload.get("snapshot_path") or ""))
        if payload.get("fingerprint") != fingerprint or not snapshot_path.is_dir():
            return None
        snapshot = PreparedSnapshotProvider(snapshot_path).load()
        if not _regional_completion(snapshot)[0]:
            return None
        return payload
    except Exception:
        return None


def _save_checkpoint(root: Path, result: Any, fingerprint: str) -> None:
    checkpoint_root = root / "_batch_checkpoints"
    checkpoint_root.mkdir(parents=True, exist_ok=True)
    path = _checkpoint_path(root, result.identity.canonical_paper_id)
    payload = result.to_dict()
    payload["fingerprint"] = fingerprint
    snapshot = getattr(result, "snapshot", None)
    complete, statuses = _regional_completion(snapshot) if snapshot is not None else (False, {})
    payload["regional_recognition_complete"] = complete
    payload["regional_recognition_status_counts"] = statuses
    temporary = path.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _resolve_local_path(value: Any, base: Path) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    return path if path.is_absolute() else base / path


def add_material_acquisition_arguments(parser: argparse.ArgumentParser, *, include_shared_budget_options: bool = True) -> None:
    """Add the same explicit acquisition/parser options to both formal CLIs."""
    parser.add_argument("--cache-root", type=Path, default=Path("data/local_material_acquisition_cache"))
    parser.add_argument("--deadline-seconds", type=float, default=180.0)
    parser.add_argument("--network-workers", type=int, default=4)
    parser.add_argument("--request-timeout-seconds", type=float, default=20.0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--disable-rescue", action="store_true", help="disable bounded public full-text rescue")
    parser.add_argument("--rescue-max-candidates", type=int, default=8, help="maximum exact public URLs considered per paper")
    parser.add_argument("--rescue-max-search-calls", type=int, default=2, help="bounded public search calls per paper")
    parser.add_argument("--rescue-max-extract-calls", type=int, default=4, help="bounded reader extraction calls per paper")
    parser.add_argument("--refresh", action="store_true", help="ignore validated cache entries and publish a new version")
    parser.add_argument("--docling-document-json", type=Path, help="opt-in saved native DoclingDocument JSON")
    parser.add_argument("--docling-run-json", type=Path, help="matching saved run.json with PDF hash, packages and pipeline options")
    parser.add_argument("--docling-cli-config", type=Path, help="explicit CLI configuration JSON; command must emit document.json and run.json")
    parser.add_argument("--docling-cache-root", type=Path, help="isolated Docling cache; defaults to acquisition cache/docling")
    parser.add_argument("--docling-max-processes", type=int, help="shared adapter local process/cache construction bound (initial default 1; maximum 8)")
    parser.add_argument("--docling-crop-scale", type=float, default=2.5, help="original-PDF crop render scale")
    parser.add_argument("--recognition-config", type=Path, help="explicit bounded formula/table transcription JSON; requires Docling; defaults to preview")
    parser.add_argument("--recognition-run", action="store_true", help="enable budgeted regional provider calls")
    parser.add_argument("--recognition-cache-root", type=Path, help="immutable regional request/response cache")
    parser.add_argument("--recognition-concurrency", type=int, help="override regional JSON concurrency (initial default 4; 1 serial; maximum 32)")
    parser.add_argument("--recognition-reviewed-failures", type=Path, help="explicit cache-key/attempt/RESULT hash allowlist; isolate only saved unknowns whose original dual holds remain consumed")
    parser.add_argument("--recognition-ceiling-cny", type=float, help="absolute cumulative project reservation ceiling for this execution")
    if include_shared_budget_options:
        parser.add_argument("--budget-ledger", type=Path, help="existing shared project ledger for regional recognition")
        parser.add_argument("--budget-limit", type=float, help="existing project budget cap; never resets its history")
    parser.add_argument("--account-ledger", type=Path, help="paired shared account ledger")
    parser.add_argument("--account-budget-limit", type=float)
    parser.add_argument("--account-mapping-dir", type=Path)
    if include_shared_budget_options:
        parser.add_argument("--key-file", type=Path)
    parser.add_argument("--key-index", type=int)
    if include_shared_budget_options:
        parser.add_argument("--tokenizer", type=Path, help="existing local tokenizer; no download")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build immutable OptoMind local material snapshots")
    parser.add_argument("--input", type=Path, help="local TEI/JATS XML or PDF")
    parser.add_argument("--pdf", type=Path, help="PDF paired with --input XML")
    parser.add_argument("--prepared", type=Path, help="existing prepared snapshot directory to validate and load")
    parser.add_argument("--record-json", type=Path, help="metadata record JSON for one acquisition")
    parser.add_argument("--batch-manifest", type=Path, help="frozen manifest JSON containing a records array")
    parser.add_argument("--output-root", type=Path, default=Path("outputs/local_materials"))
    parser.add_argument("--canonical-paper-id", default="")
    parser.add_argument("--publication-version", default="")
    parser.add_argument("--source-document-id", default="main")
    parser.add_argument("--source-role", default="main")
    parser.add_argument("--source-uri", default="")
    add_material_acquisition_arguments(parser)
    return parser


def make_material_acquirer_factory(args: argparse.Namespace, *, regional_client_constructor=None) -> Callable[[Path], MaterialAcquirer]:
    """Build parser/provider instances once; per-source acquirers share their bounds.

    This is explicit configuration, not runtime-file discovery. Construction
    never reads keys or sends requests. The optional constructor preserves the
    native factory accounting while a caller adds its supervised transport.
    """
    args = argparse.Namespace(**vars(args))
    args.budget_limit = getattr(args, "budget_limit", getattr(args, "budget_limit_cny", None))
    config = AcquisitionConfig(deadline_seconds=args.deadline_seconds, network_workers=args.network_workers, request_timeout_seconds=args.request_timeout_seconds, max_retries=args.max_retries, cache_root=args.cache_root, refresh=args.refresh, rescue_enabled=not args.disable_rescue, rescue_max_candidates=args.rescue_max_candidates, rescue_max_search_calls=args.rescue_max_search_calls, rescue_max_extract_calls=args.rescue_max_extract_calls)
    providers: dict[str, Any] = {}
    if args.docling_document_json or args.docling_run_json or args.docling_cli_config:
        from optomind_research.runtime.upgrade3.docling_adapter import DoclingAdapterConfig, DoclingCliAdapter, config_sha256

        cache_root = args.docling_cache_root or args.cache_root / "docling"
        if args.docling_cli_config:
            if args.docling_document_json or args.docling_run_json:
                raise ValueError("choose saved Docling files or explicit CLI configuration")
            value = _read_json(args.docling_cli_config)
            adapter_config = DoclingAdapterConfig(parser_version=str(value["parser_version"]), cache_root=cache_root,
                command=tuple(value["command"]), command_args=tuple(value.get("command_args") or ()),
                config=dict(value["config"]), timeout_seconds=float(value.get("timeout_seconds", args.deadline_seconds)),
                output_filename=str(value.get("output_filename") or "document.json"), crop_scale=args.docling_crop_scale,
                max_processes=args.docling_max_processes if args.docling_max_processes is not None else value.get("max_processes", 1))
        else:
            if not (args.docling_document_json and args.docling_run_json):
                raise ValueError("saved Docling import requires both --docling-document-json and --docling-run-json")
            adapter_config = DoclingAdapterConfig.from_saved_run(args.docling_document_json, args.docling_run_json,
                cache_root=cache_root, crop_scale=args.docling_crop_scale,
                max_processes=args.docling_max_processes if args.docling_max_processes is not None else 1)
        recognizer = None
        if args.recognition_config:
            from optomind_research.runtime.upgrade3.regional_recognition import RegionalRecognitionConfig, RegionalRecognizer, vision_token_counter
            from scripts.upgrade3.writer_candidates import make_live_factory, tokenizer_counter

            recognition_options = _read_json(args.recognition_config)
            if not isinstance(recognition_options, Mapping) or "cache_root" in recognition_options or "scope" in recognition_options:
                raise ValueError("recognition_config_requires_bounded_options_object")
            recognition_options = dict(recognition_options)
            if args.recognition_concurrency is not None:
                recognition_options["concurrency"] = args.recognition_concurrency
            factory = None
            tokenizer_info = {}
            if args.recognition_run:
                if not all((args.budget_ledger, args.account_ledger, args.tokenizer, args.key_file, args.recognition_ceiling_cny)) or args.key_index is None or args.key_index < 1:
                    raise ValueError("regional_live_requires_project_account_ledgers_tokenizer_key_and_explicit_ceiling")
                if not args.budget_ledger.is_file() or not args.account_ledger.is_file():
                    raise ValueError("regional_live_requires_existing_ledgers")
                counter, tokenizer_info = tokenizer_counter(args.tokenizer)
                args.run, args.responses, args.allow_max = True, None, False
                factory = make_live_factory(args, token_counter=vision_token_counter(counter), reservation_ceiling_cny=args.recognition_ceiling_cny, thread_safe=True, client_constructor=regional_client_constructor)
            recognition_options["scope"] = config_sha256({"project_ledger": str(args.budget_ledger.resolve()) if args.budget_ledger else "",
                "account_ledger": str(args.account_ledger.resolve()) if args.account_ledger else "", "tokenizer": tokenizer_info,
                "reservation_ceiling_cny": args.recognition_ceiling_cny})
            recognition_config = RegionalRecognitionConfig(cache_root=args.recognition_cache_root or args.cache_root / "regional_recognition", **recognition_options)
            recognizer = RegionalRecognizer(recognition_config, provider_factory=factory, reviewed_failures=args.recognition_reviewed_failures)
        providers["docling"] = DoclingCliAdapter(adapter_config, recognizer=recognizer)
        config.docling_enabled = True
        config.docling_config_sha256 = config_sha256({"parser_version": adapter_config.parser_version, **adapter_config.fingerprint(),
            "recognition": {"request": recognizer.config.fingerprint(), "policy": recognizer.config.policy()} if recognizer else None,
            "local_scheduling": {"max_processes": adapter_config.max_processes}, "recognition_run": args.recognition_run})
    elif args.recognition_config or args.recognition_run:
        raise ValueError("regional recognition requires an explicit Docling configuration")
    if args.recognition_run and not args.recognition_config:
        raise ValueError("--recognition-run requires --recognition-config")
    if args.recognition_reviewed_failures and not args.recognition_run:
        raise ValueError("--recognition-reviewed-failures requires --recognition-run")
    reader = FirecrawlReader(timeout_seconds=min(config.request_timeout_seconds, 20.0)) if config.rescue_enabled and (config.rescue_max_search_calls or config.rescue_max_extract_calls) else None

    def factory(material_root: Path) -> MaterialAcquirer:
        return MaterialAcquirer(material_root, config=config, providers=providers, reader=reader)
    return factory


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    modes = sum(bool(item) for item in (args.input, args.prepared, args.batch_manifest)) + (1 if args.record_json and not args.input else 0)
    if modes != 1:
        _build_parser().error("choose exactly one of --input, --prepared, --record-json, or --batch-manifest")

    if args.prepared:
        snapshot = PreparedSnapshotProvider(args.prepared).load()
        _print({"status": "loaded", "snapshot_id": snapshot.snapshot_id, "path": str(snapshot.root), "content_depth": snapshot.content_depth})
        return 0

    try:
        acquirer = make_material_acquirer_factory(args)(args.output_root)
    except ValueError as exc:
        parser.error(str(exc))
    config = acquirer.config

    if args.batch_manifest:
        manifest_path = args.batch_manifest
        payload = _read_json(manifest_path)
        records = payload.get("records") if isinstance(payload, Mapping) else payload
        if not isinstance(records, list):
            raise SystemExit("batch_manifest_records_must_be_list")
        prepared_records: list[tuple[Mapping[str, Any], bytes | None, bytes | None, str]] = []
        for item in records:
            if not isinstance(item, Mapping):
                continue
            row = dict(item)
            xml_path = _resolve_local_path(row.get("local_xml") or row.get("xml_path"), manifest_path.parent)
            pdf_path = _resolve_local_path(row.get("local_pdf") or row.get("pdf_path"), manifest_path.parent)
            xml_bytes = xml_path.read_bytes() if xml_path and xml_path.is_file() else None
            pdf_bytes = pdf_path.read_bytes() if pdf_path and pdf_path.is_file() else None
            prepared_records.append((row, xml_bytes, pdf_bytes, xml_path.name if xml_path else "source.xml"))
        network_records: list[Mapping[str, Any]] = []
        local_records = []
        network_fingerprints: dict[str, str] = {}
        completed = 0
        for row, xml_bytes, pdf_bytes, source_name in prepared_records:
            paper_id = row.get("canonical_paper_id") or row.get("paper_id") or row.get("doi") or row.get("title") or "paper"
            fingerprint = _fingerprint(row, config, xml_bytes=xml_bytes or b"", pdf_bytes=pdf_bytes or b"")
            resumed = _load_resume(args.output_root, paper_id, fingerprint, refresh=args.refresh)
            if resumed is not None:
                _print({"paper_id": paper_id, "status": "resumed", "material_depth": resumed.get("material_depth", ""), "source": resumed.get("selected_route", ""), "snapshot_id": resumed.get("snapshot_id", ""), "snapshot_path": resumed.get("snapshot_path", ""), "elapsed_seconds": resumed.get("elapsed_seconds", 0), "errors": resumed.get("errors", [])})
                completed += 1
                continue
            if not (xml_bytes or pdf_bytes):
                network_records.append(row)
                network_fingerprints[_safe_slug(paper_id)] = fingerprint
                for alias in (row.get("canonical_paper_id"), row.get("paper_id"), row.get("doi"), row.get("title")):
                    if alias:
                        network_fingerprints[_safe_slug(alias)] = fingerprint
                continue
            local_records.append((row, xml_bytes, pdf_bytes, source_name, fingerprint))

        # Reuse the existing outer worker bound and one acquirer. Its shared
        # Docling/recognizer instances enforce separate global inner bounds.
        with ThreadPoolExecutor(max_workers=config.network_workers) as pool:
            local_futures = {
                pool.submit(acquirer.acquire_local, row, xml_bytes=xml_bytes, pdf_bytes=pdf_bytes,
                    source_name=source_name, source_document_id=args.source_document_id, source_role=args.source_role,
                    source_uri=args.source_uri, publication_version=args.publication_version): (row, fingerprint)
                for row, xml_bytes, pdf_bytes, source_name, fingerprint in local_records
            }
            for future in as_completed(local_futures):
                row, fingerprint = local_futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = AcquisitionResult(identity=acquirer._record_identity(row), status="failed", material_depth="unknown",
                        errors=[f"{type(exc).__name__}:{exc}"])
                _save_checkpoint(args.output_root, result, fingerprint)
                _print(_summary(result))
                completed += 1

        def on_result(result: Any) -> None:
            nonlocal completed
            fingerprint = network_fingerprints.get(_safe_slug(result.identity.canonical_paper_id), "")
            if not fingerprint:
                fingerprint = network_fingerprints.get(_safe_slug(result.identity.doi), "") or network_fingerprints.get(_safe_slug(result.identity.title), "")
            _save_checkpoint(args.output_root, result, fingerprint)
            _print(_summary(result))
            completed += 1

        acquirer.acquire_batch(network_records, on_result=on_result)
        _print({"status": "batch_complete", "records": completed, "output_root": str(args.output_root)})
        return 0

    if args.input:
        data = args.input.read_bytes()
        record = _record(args.record_json, args.canonical_paper_id)
        if not record.get("canonical_paper_id"):
            record["canonical_paper_id"] = args.canonical_paper_id or args.input.stem
        is_pdf = args.input.suffix.casefold() == ".pdf" or data.startswith(b"%PDF")
        result = acquirer.acquire_local(record, pdf_bytes=data if is_pdf else (args.pdf.read_bytes() if args.pdf else None), xml_bytes=None if is_pdf else data, source_name=args.input.name, source_document_id=args.source_document_id, source_role=args.source_role, source_uri=args.source_uri, publication_version=args.publication_version)
        _print(_summary(result))
        return 0 if result.status == "published" else 1

    record = _record(args.record_json, args.canonical_paper_id)
    result = acquirer.acquire(record)
    _print(_summary(result))
    return 0 if result.status == "published" else 1


if __name__ == "__main__":
    raise SystemExit(main())
