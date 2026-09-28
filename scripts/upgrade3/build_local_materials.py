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
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from optomind_research.runtime.upgrade3.document_snapshot import NORMALIZER_VERSION
from optomind_research.runtime.upgrade3.local_materials import PreparedSnapshotProvider
from optomind_research.runtime.upgrade3.material_acquisition import ACQUISITION_SCHEMA_VERSION, AcquisitionConfig, MaterialAcquirer
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
        PreparedSnapshotProvider(snapshot_path).load()
        return payload
    except Exception:
        return None


def _save_checkpoint(root: Path, result: Any, fingerprint: str) -> None:
    checkpoint_root = root / "_batch_checkpoints"
    checkpoint_root.mkdir(parents=True, exist_ok=True)
    path = _checkpoint_path(root, result.identity.canonical_paper_id)
    payload = result.to_dict()
    payload["fingerprint"] = fingerprint
    temporary = path.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _resolve_local_path(value: Any, base: Path) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    return path if path.is_absolute() else base / path


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    modes = sum(bool(item) for item in (args.input, args.prepared, args.batch_manifest)) + (1 if args.record_json and not args.input else 0)
    if modes != 1:
        _build_parser().error("choose exactly one of --input, --prepared, --record-json, or --batch-manifest")

    if args.prepared:
        snapshot = PreparedSnapshotProvider(args.prepared).load()
        _print({"status": "loaded", "snapshot_id": snapshot.snapshot_id, "path": str(snapshot.root), "content_depth": snapshot.content_depth})
        return 0

    config = AcquisitionConfig(deadline_seconds=args.deadline_seconds, network_workers=args.network_workers, request_timeout_seconds=args.request_timeout_seconds, max_retries=args.max_retries, cache_root=args.cache_root, refresh=args.refresh, rescue_enabled=not args.disable_rescue, rescue_max_candidates=args.rescue_max_candidates, rescue_max_search_calls=args.rescue_max_search_calls, rescue_max_extract_calls=args.rescue_max_extract_calls)
    reader = FirecrawlReader(timeout_seconds=min(config.request_timeout_seconds, 20.0)) if config.rescue_enabled and (config.rescue_max_search_calls or config.rescue_max_extract_calls) else None
    acquirer = MaterialAcquirer(args.output_root, config=config, reader=reader)

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
            result = acquirer.acquire_local(row, xml_bytes=xml_bytes, pdf_bytes=pdf_bytes, source_name=source_name, source_document_id=args.source_document_id, source_role=args.source_role, source_uri=args.source_uri, publication_version=args.publication_version)
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
