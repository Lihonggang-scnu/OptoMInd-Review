"""Run an explicitly configured local Docling conversion, without downloads."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import os
import time
from pathlib import Path


def _canonical(value):
    def stable(item, key=""):
        if isinstance(item, dict):
            return {name: stable(child, name) for name, child in item.items()}
        if isinstance(item, list):
            values = [stable(child) for child in item]
            return sorted(values) if key == "supported_engines" else values
        return item
    return json.dumps(stable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def native_pipeline_options(options):
    """Rebuild concrete SDK options; base-typed JSON loses their ``kind``.

    Only the SDK's native defaults are supported by this bounded runner. A
    declared option kind may not select a plugin or a different backend.
    serialize_as_any is essential: the SDK annotates several fields by base
    classes and an ordinary dump silently drops concrete model/engine fields.
    """
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    defaults = PdfPipelineOptions()
    prepared = copy.deepcopy(options)
    option_types = {}
    for name in getattr(PdfPipelineOptions, "model_fields", {}):
        default = getattr(defaults, name)
        if not hasattr(default, "model_dump"):
            continue
        concrete = type(default)
        option_types[name] = concrete.__name__
        declared = prepared.get(name, {})
        if not isinstance(declared, dict):
            raise ValueError("native_option_object_required:" + name)
        if declared.get("kind") not in (None, getattr(concrete, "kind", None)):
            raise ValueError("unsupported_native_option_kind:" + name)
        complete = default.model_dump(mode="json", serialize_as_any=True)
        complete.update(declared)
        prepared[name] = concrete.model_validate(copy.deepcopy(complete))
    pipeline = PdfPipelineOptions.model_validate(prepared)
    return pipeline, option_types


def resolve_page_range(value, input_path):
    if value in (None, "all"):
        from pypdfium2 import PdfDocument
        document = PdfDocument(str(input_path))
        try:
            return [1, len(document)]
        finally:
            document.close()
    if not isinstance(value, list) or len(value) != 2 or not all(isinstance(v, int) and not isinstance(v, bool) for v in value) or not 1 <= value[0] <= value[1]:
        raise ValueError("page_range must contain original PDF page start and end, or all")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True, help="packages, full pipeline_options and original page_range")
    parser.add_argument("--preflight-only", action="store_true", help="validate installed packages/options without loading models")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    options = config["pipeline_options"]
    for key in ("do_ocr", "do_formula_enrichment", "do_code_enrichment", "do_chart_extraction",
                "do_picture_classification", "do_picture_description", "enable_remote_services", "allow_external_plugins"):
        if options.get(key):
            parser.error(f"{key} is outside this local layout runner")
    artifacts = Path(options["artifacts_path"])
    if not artifacts.is_dir():
        parser.error("configured local artifacts_path must already exist")
    if any((args.output / name).exists() for name in ("document.json", "run.json")):
        parser.error("output already contains a Docling result; choose a new directory")
    packages = {}
    for name in config["packages"]:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    if packages != config["packages"]:
        parser.error("installed package versions differ from the configured fingerprint")
    page_range = resolve_page_range(config.get("page_range"), args.input)
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
        os.environ[key] = "1"
    # Heavy imports and model loading occur only after explicit input checks.
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter, PdfFormatOption

    # Docling's OCR validator consumes keys from its input mapping. Preserve
    # the declared options for comparison and the command fingerprint.
    pipeline, option_types = native_pipeline_options(options)
    actual_options = pipeline.model_dump(mode="json", serialize_as_any=True)
    if _canonical(actual_options) != _canonical(options):
        parser.error("resolved pipeline options differ from the configured fingerprint")
    if args.preflight_only:
        print(json.dumps({"status": "preflight_passed", "config_sha256": hashlib.sha256(_canonical(config).encode()).hexdigest()}, ensure_ascii=True))
        return 0
    args.output.mkdir(parents=True, exist_ok=True)
    data = args.input.read_bytes()
    started = time.monotonic()
    run = {"status": "started", "source_pdf": str(args.input.resolve()),
           "source_sha256": hashlib.sha256(data).hexdigest(), "source_bytes": len(data),
           "packages": packages, "pipeline_options": actual_options, "page_range": page_range,
           "configured_page_range": config.get("page_range", "all"), "native_option_types": option_types,
           "offline": True, "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    if config.get("model_manifest") is not None:
        run["model_manifest"] = config["model_manifest"]
    try:
        converter = DocumentConverter(allowed_formats=[InputFormat.PDF],
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline)})
        converted = converter.convert(args.input, raises_on_error=True, page_range=tuple(page_range))
        payload = converted.document.export_to_dict()
        (args.output / "document.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        conversion_status = getattr(converted.status, "value", str(converted.status))
        errors = [error.model_dump(mode="json") if hasattr(error, "model_dump") else str(error)
                  for error in (getattr(converted, "errors", None) or ())]
        run.update({"status": "success" if conversion_status == "success" else "partial",
                    "conversion_status": conversion_status, "errors": errors, "num_pages": converted.document.num_pages()})
    except Exception as exc:
        run.update({"status": "error", "error": f"{type(exc).__name__}:{exc}"})
        raise
    finally:
        run["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (args.output / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: run[key] for key in ("status", "source_sha256", "num_pages")}, ensure_ascii=True))
    return 0 if run["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
